"""Prueba de humo de las fixtures de integración (TASK-103, LC-NUC-36).

Comprueba que ``postgres_endpoint`` es PostgreSQL 16 en UTC y que ``localstack_endpoint`` sirve
KMS y Secrets Manager, además de S3 (``test_localstack_checksum.py``). Corre igual con
testcontainers y con ``VIGIA_TEST_USE_COMPOSE=1``. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import secrets
import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_is_version_16_in_utc(postgres_endpoint: PostgresEndpoint) -> None:
    connection = await asyncpg.connect(postgres_endpoint.dsn, timeout=10)
    try:
        version_num = int(await connection.fetchval("SHOW server_version_num"))
        timezone = await connection.fetchval("SHOW TimeZone")
    finally:
        await connection.close()
    assert version_num // 10000 == 16
    assert timezone == "UTC"


@pytest.mark.asyncio
async def test_sqlalchemy_url_opens_an_async_engine(postgres_endpoint: PostgresEndpoint) -> None:
    engine = create_async_engine(postgres_endpoint.sqlalchemy_url)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


def test_kms_encrypts_and_decrypts(localstack_endpoint: LocalStackEndpoint) -> None:
    kms = localstack_endpoint.aws_client("kms")
    key_id = kms.create_key(Description="vigia-task-103-humo")["KeyMetadata"]["KeyId"]
    plaintext = secrets.token_bytes(32)
    ciphertext = kms.encrypt(KeyId=key_id, Plaintext=plaintext)["CiphertextBlob"]
    assert ciphertext != plaintext
    assert kms.decrypt(CiphertextBlob=ciphertext)["Plaintext"] == plaintext


def test_secrets_manager_stores_and_returns_a_secret(
    localstack_endpoint: LocalStackEndpoint,
) -> None:
    client = localstack_endpoint.aws_client("secretsmanager")
    name = f"vigia/local/humo-{uuid.uuid4().hex[:12]}"
    value = secrets.token_urlsafe(24)
    client.create_secret(Name=name, SecretString=value)
    try:
        assert client.get_secret_value(SecretId=name)["SecretString"] == value
    finally:
        client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
