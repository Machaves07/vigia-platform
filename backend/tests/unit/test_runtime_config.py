"""``RuntimeConfig`` y el secreto de la base de la raíz de composición (VIG-137; ERR-019).

- Una configuración completa se lee una vez, con los valores del diseño por defecto (pendiente
  nº 17 de U-03: pools 10 y 5, sin desbordamiento, espera de 5 s, 4 hilos, mamparos 35 y 15).
- Cada variable obligatoria ausente (o vacía) detiene la lectura con ``RuntimeConfigInvalid``,
  que **nombra la variable**.
- Un valor no válido también, y el mensaje **nunca contiene el valor** (propiedad con Hypothesis
  sobre valores hostiles: marcas de secreto, espacios, caracteres invisibles, homoglifos,
  dígitos no ASCII y cadenas enormes).
- Bordes: tamaños en su mínimo, máximo y justo fuera (los de los mamparos, con su reserva del
  30 %, en ``test_bulkheads.py``); ``VIGIA_DB_MAX_OVERFLOW`` solo admite 0;
  ``VIGIA_AWS_ENDPOINT_URL`` solo en ``local`` y ``test``; el prefijo de firma da el entorno de
  los secretos.
- ``parse_database_secret``: el formato de RDS; un secreto incompleto nombra el campo y nunca un
  valor; la contraseña no sale en el ``repr``.

Solo datos generados.
"""

from __future__ import annotations

import json
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.runtime_support import SECRET_PASSWORD, database_secret, runtime_environ
from vigia_platform.shared.db import SslMode
from vigia_platform.shared.runtime.config import VARIABLES, RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.runtime.db_credentials import (
    DatabaseSecretInvalid,
    database_url,
    parse_database_secret,
)

REQUIRED = [variable.name for variable in VARIABLES if variable.required]
ALL = [variable.name for variable in VARIABLES]
MARK = "SECRETO-NO-SALE"


def _read(**changes: str | None) -> RuntimeConfig:
    return RuntimeConfig.from_environ(runtime_environ(**changes))


def _invalid(variable: str, value: str) -> RuntimeConfigInvalid:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _read(**{variable: value})
    return raised.value


# --- Lectura -------------------------------------------------------------------------------------


def test_a_complete_configuration_is_read_once_with_the_design_defaults() -> None:
    config = _read()
    assert config.environment == "test"
    assert config.db_app_secret == "vigia/test/db/app"  # noqa: S105 - nombre, no valor
    assert config.provider_organization_id == uuid.UUID(
        runtime_environ()["VIGIA_PROVIDER_ORGANIZATION_ID"]
    )
    assert (config.db_pool_node, config.db_pool_person, config.db_max_overflow) == (10, 5, 0)
    assert config.db_pool_timeout_seconds == 5
    assert config.db_statement_timeout_ms is None  # el del proceso
    assert (config.threadpool_size, config.bulkhead_node, config.bulkhead_person) == (4, 35, 15)
    assert config.db_sslmode is SslMode.DISABLE
    assert config.signing_environment == "test"
    with pytest.raises(ValidationError):
        config.environment = "pilot"  # type: ignore[misc]


def test_the_pilot_template_values_are_accepted() -> None:
    config = _read(
        VIGIA_ENVIRONMENT="pilot",
        PGSSLMODE=None,
        VIGIA_SIGNING_SECRET_PREFIX="vigia/pilot/signing/",  # noqa: S106 - prefijo de nombres
        VIGIA_DB_POOL_NODE="10",
        VIGIA_DB_POOL_PERSON="5",
        VIGIA_DB_MAX_OVERFLOW="0",
        VIGIA_DB_POOL_TIMEOUT_SECONDS="5",
        VIGIA_DB_STATEMENT_TIMEOUT_MS="30000",
        VIGIA_THREADPOOL_SIZE="4",
        VIGIA_BULKHEAD_NODE="35",
        VIGIA_BULKHEAD_PERSON="15",
        VIGIA_UVICORN_WORKERS="2",
        VIGIA_CRL_KEY="ca/crl.pem",
        VIGIA_EDGE_BUCKET="vigia-edge-123456789012-us-east-1",
        VIGIA_NODE_CA_KEY_ARN="arn:aws:kms:us-east-1:123456789012:key/abc-123",
        VIGIA_NODE_TRUST_STORE_ARN=(
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:truststore/vigia-node-trust/abc"
        ),
    )
    assert config.db_sslmode is SslMode.VERIFY_FULL  # sin PGSSLMODE
    assert config.db_statement_timeout_ms == 30_000
    assert config.signing_environment == "pilot"


@pytest.mark.parametrize("variable", REQUIRED)
@pytest.mark.parametrize("absent", [None, ""])
def test_a_missing_required_variable_is_named(variable: str, absent: str | None) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _read(**{variable: absent})
    assert raised.value.variable == variable
    assert variable in str(raised.value)


def test_require_names_the_variable_of_the_process() -> None:
    config = _read(VIGIA_ARCHIVE_BUCKET=None)
    with pytest.raises(RuntimeConfigInvalid) as raised:
        config.require("archive_bucket")
    assert raised.value.variable == "VIGIA_ARCHIVE_BUCKET"
    assert config.require("evidence_bucket") == "vigia-evidence-test"


def test_unknown_variables_are_ignored() -> None:
    assert _read(VIGIA_SOMETHING_ELSE="x").environment == "test"


# --- Valores no válidos sin repetir el valor (ERR-019) -------------------------------------------


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VIGIA_ENVIRONMENT", "produccion"),
        ("VIGIA_ENVIRONMENT", "staging-0"),
        ("VIGIA_ENVIRONMENT", " test"),
        ("AWS_REGION", "us-east-1 "),
        ("VIGIA_DB_APP_SECRET", "vigia/test/db/app;" + MARK),
        ("VIGIA_DB_APP_SECRET", "/vigia/" + MARK),
        ("VIGIA_SIGNING_SECRET_PREFIX", "vigia/test/signing"),
        ("VIGIA_SIGNING_SECRET_PREFIX", "vigia/Test/signing/"),
        ("VIGIA_SECRETS_KEY_ARN", "alias/" + MARK + " x"),
        ("VIGIA_PROVIDER_ORGANIZATION_ID", "0192F2B0-0000-4000-8000-0000000000AB"),
        ("VIGIA_PROVIDER_ORGANIZATION_ID", "{0192f2b0-0000-4000-8000-0000000000ab}"),
        ("VIGIA_EVIDENCE_BUCKET", "Vigia_Evidence"),
        ("VIGIA_CRL_KEY", "../ca/crl.pem"),
        ("VIGIA_NODE_TRUST_STORE_ARN", "truststore/" + MARK),
        ("PGSSLMODE", "prefer"),
        ("PGSSLROOTCERT", "relativa/rds.pem"),
        ("VIGIA_BREACH_LIST_PATH", "pwned.txt"),
        ("VIGIA_DB_POOL_NODE", "0"),
        ("VIGIA_DB_POOL_NODE", "101"),
        ("VIGIA_DB_POOL_NODE", "+5"),
        ("VIGIA_DB_POOL_NODE", " 5"),
        ("VIGIA_DB_POOL_NODE", chr(0x0665)),  # dígito cinco árabe-índico (no ASCII)
        ("VIGIA_DB_POOL_NODE", "9" * 400),
        ("VIGIA_DB_MAX_OVERFLOW", "1"),
        ("VIGIA_DB_POOL_TIMEOUT_SECONDS", "61"),
        ("VIGIA_DB_STATEMENT_TIMEOUT_MS", "99"),
        ("VIGIA_THREADPOOL_SIZE", "65"),
        ("VIGIA_UVICORN_WORKERS", "0"),
        ("VIGIA_AWS_ENDPOINT_URL", "ftp://localstack:4566"),
    ],
)
def test_an_invalid_value_is_named_without_its_value(variable: str, value: str) -> None:
    error = _invalid(variable, value)
    assert error.variable == variable
    assert variable in str(error)
    if len(value) >= 4:
        assert value not in str(error) and value not in repr(error)


@pytest.mark.parametrize(
    ("variable", "low", "high"),
    [
        ("VIGIA_DB_POOL_NODE", "1", "100"),
        ("VIGIA_DB_POOL_PERSON", "1", "100"),
        ("VIGIA_DB_MAX_OVERFLOW", "0", "0"),
        ("VIGIA_DB_POOL_TIMEOUT_SECONDS", "1", "60"),
        ("VIGIA_DB_STATEMENT_TIMEOUT_MS", "100", "600000"),
        ("VIGIA_THREADPOOL_SIZE", "1", "64"),
        ("VIGIA_UVICORN_WORKERS", "1", "16"),
    ],
)
def test_sizes_accept_their_bounds(variable: str, low: str, high: str) -> None:
    field = next(v.field for v in VARIABLES if v.name == variable)
    assert getattr(_read(**{variable: low}), field) == int(low)
    assert getattr(_read(**{variable: high}), field) == int(high)
    _invalid(variable, str(int(high) + 1))
    if int(low) > 0:
        _invalid(variable, str(int(low) - 1))


@given(
    variable=st.sampled_from(ALL),
    noise=st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=40),
    big=st.booleans(),
)
def test_no_rejected_value_is_ever_echoed(variable: str, noise: str, big: bool) -> None:
    # Una marca de secreto con ruido alrededor (espacios, invisibles, homoglifos…): nunca es
    # válida en ninguna variable, y el mensaje solo nombra la variable.
    invisible, no_break = chr(0x200B), chr(0x00A0)
    value = f"{noise}{invisible}{MARK}{no_break}{noise}" + ("x" * 70_000 if big else "")
    error = _invalid(variable, value)
    assert error.variable == variable
    assert MARK not in str(error)
    assert MARK not in repr(error)


@pytest.mark.parametrize("environment", ["pilot", "staging-3"])
def test_the_endpoint_override_is_rejected_outside_local_and_test(environment: str) -> None:
    error = _invalid_with(
        VIGIA_ENVIRONMENT=environment,
        VIGIA_AWS_ENDPOINT_URL="http://localstack:4566",
    )
    assert error.variable == "VIGIA_AWS_ENDPOINT_URL"


@pytest.mark.parametrize("environment", ["local", "test"])
def test_the_endpoint_override_is_accepted_in_local_and_test(environment: str) -> None:
    config = _read(VIGIA_ENVIRONMENT=environment, VIGIA_AWS_ENDPOINT_URL="http://localhost:4566")
    assert config.aws_endpoint_url == "http://localhost:4566"


def _invalid_with(**changes: str) -> RuntimeConfigInvalid:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _read(**changes)
    return raised.value


# --- Secreto de la base --------------------------------------------------------------------------


def test_the_rds_secret_is_parsed_and_the_url_has_no_credential() -> None:
    secret = parse_database_secret(database_secret(host="db.internal", port="5432", dbname="vigia"))
    assert (secret.host, secret.port, secret.dbname, secret.username) == (
        "db.internal",
        5432,
        "vigia",
        "vigia_app",
    )
    assert secret.credential == ("vigia_app", SECRET_PASSWORD)
    assert SECRET_PASSWORD not in repr(secret)
    url = database_url(secret)
    assert url == "postgresql+asyncpg://db.internal:5432/vigia"
    assert SECRET_PASSWORD not in url and "vigia_app" not in url


@pytest.mark.parametrize(
    "document",
    [
        database_secret(host=None),
        database_secret(password=None),
        database_secret(port=None),
        database_secret(port=0),
        database_secret(port=70000),
        database_secret(port=True),
        database_secret(port="54 32"),
        database_secret(host=""),
        database_secret(username=["vigia_app"]),
        database_secret(host="db\n.internal"),
        json.dumps([SECRET_PASSWORD]),
        "no es JSON " + SECRET_PASSWORD,
        "",
        "x" * 70_000 + SECRET_PASSWORD,
    ],
)
def test_an_incomplete_secret_names_the_field_never_a_value(document: str) -> None:
    with pytest.raises(DatabaseSecretInvalid) as raised:
        parse_database_secret(document)
    assert SECRET_PASSWORD not in str(raised.value)
    assert "db.vigia.invalid" not in str(raised.value)
