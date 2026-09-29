"""Conexión y contraseñas de rol del trabajo de migración (TASK-106; infrastructure-design §5.4).

Con un lector de secretos doble: el modo local (variables PG* y contraseñas locales), el de AWS
con las variables que fija ``infra/stacks/compute.py`` (primer despliegue y siguientes) y los
secretos mal formados, siempre sin que un valor aparezca en el mensaje. Con Secrets Manager de
LocalStack y PostgreSQL real, en ``tests/integration/test_roles.py``.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    APP_SECRET_VARIABLE,
    MASTER_SECRET_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
    MIGRATE_SECRET_VARIABLE,
    MigrationCredentialsError,
    resolve_migration_credentials,
)

APP_VALUE = "app-value-0123456789abcdef"
MIGRATE_VALUE = "migrate-value-0123456789ab"
MASTER_VALUE = "master-value-0123456789abc"

MIGRATE_SECRET = {
    "engine": "postgres",
    "host": "vigia-pilot-db.example.internal",
    "port": 5432,
    "dbname": "vigia",
    "username": "vigia_migrate",
    "password": MIGRATE_VALUE,
}
MIGRATE_ID = "vigia/pilot/db/migrate"
APP_ID = "vigia/pilot/db/app"
MASTER_ID = "arn:aws:secretsmanager:us-east-1:111122223333:secret:rds!db-1"
SECRETS = {
    MIGRATE_ID: json.dumps(MIGRATE_SECRET),
    APP_ID: json.dumps({**MIGRATE_SECRET, "username": "vigia_app", "password": APP_VALUE}),
    MASTER_ID: json.dumps({"username": "vigia_owner", "password": MASTER_VALUE}),
}
FIRST_DEPLOY = {
    MIGRATE_SECRET_VARIABLE: MIGRATE_ID,
    APP_SECRET_VARIABLE: APP_ID,
    MASTER_SECRET_VARIABLE: MASTER_ID,
}


class RecordingFetcher:
    """Lector de secretos doble que anota qué secretos se pidieron."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets
        self.calls: list[str] = []

    def __call__(self, secret_id: str) -> str:
        self.calls.append(secret_id)
        return self.secrets[secret_id]


def test_local_mode_uses_libpq_variables_and_local_passwords() -> None:
    credentials = resolve_migration_credentials(
        {APP_PASSWORD_VARIABLE: APP_VALUE, MIGRATE_PASSWORD_VARIABLE: MIGRATE_VALUE, "PGHOST": "h"}
    )
    assert credentials.source == "local"
    # URL vacía: asyncpg completa host, usuario, contraseña y base con las variables PG*.
    assert credentials.url.render_as_string(hide_password=False) == "postgresql+asyncpg://"
    assert credentials.role_passwords == {"vigia_app": APP_VALUE, "vigia_migrate": MIGRATE_VALUE}


def test_local_mode_without_passwords_leaves_them_out() -> None:
    """Sin contraseñas locales, ``nuc_0001`` falla con su mensaje; las siguientes no las piden."""
    assert resolve_migration_credentials({APP_PASSWORD_VARIABLE: ""}).role_passwords == {}


def test_first_deploy_logs_in_as_master_and_gets_both_role_passwords() -> None:
    fetch = RecordingFetcher(SECRETS)
    credentials = resolve_migration_credentials(FIRST_DEPLOY, fetch)
    url = credentials.url
    assert credentials.source == "aws"
    assert (url.username, url.password, url.host, url.port, url.database) == (
        "vigia_owner",
        MASTER_VALUE,
        "vigia-pilot-db.example.internal",
        5432,
        "vigia",
    )
    assert credentials.role_passwords == {"vigia_app": APP_VALUE, "vigia_migrate": MIGRATE_VALUE}
    assert MASTER_VALUE not in repr(credentials) and APP_VALUE not in repr(credentials)


def test_later_deploys_log_in_as_vigia_migrate() -> None:
    fetch = RecordingFetcher(SECRETS)
    credentials = resolve_migration_credentials({MIGRATE_SECRET_VARIABLE: MIGRATE_ID}, fetch)
    assert (credentials.url.username, credentials.url.password) == ("vigia_migrate", MIGRATE_VALUE)
    assert credentials.role_passwords == {"vigia_migrate": MIGRATE_VALUE}
    assert fetch.calls == [MIGRATE_ID]


def test_aws_mode_ignores_local_password_variables() -> None:
    environ = {**FIRST_DEPLOY, APP_PASSWORD_VARIABLE: "local-value-should-not-win"}
    credentials = resolve_migration_credentials(environ, RecordingFetcher(SECRETS))
    assert credentials.role_passwords["vigia_app"] == APP_VALUE


@pytest.mark.parametrize(
    ("secret", "missing"),
    [
        ({k: v for k, v in MIGRATE_SECRET.items() if k != "host"}, "'host'"),
        ({**MIGRATE_SECRET, "port": "54x"}, "'port'"),
        ({**MIGRATE_SECRET, "port": True}, "'port'"),
        ({**MIGRATE_SECRET, "password": ""}, "'password'"),
        ({**MIGRATE_SECRET, "dbname": 7}, "'dbname'"),
    ],
)
def test_malformed_migrate_secret_is_rejected_without_values(
    secret: dict[str, object], missing: str
) -> None:
    fetch = RecordingFetcher({"m": json.dumps(secret)})
    with pytest.raises(MigrationCredentialsError) as caught:
        resolve_migration_credentials({MIGRATE_SECRET_VARIABLE: "m"}, fetch)
    assert missing in str(caught.value)
    assert MIGRATE_VALUE not in str(caught.value)


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '"text"', "null"])
def test_secret_that_is_not_a_json_object_is_rejected(raw: str) -> None:
    with pytest.raises(MigrationCredentialsError, match="no es un objeto JSON"):
        resolve_migration_credentials({MIGRATE_SECRET_VARIABLE: "m"}, RecordingFetcher({"m": raw}))


@pytest.mark.parametrize("port", [0, 65536, "70000"])
def test_port_out_of_range_is_rejected(port: object) -> None:
    fetch = RecordingFetcher({"m": json.dumps({**MIGRATE_SECRET, "port": port})})
    with pytest.raises(MigrationCredentialsError, match="puerto fuera de rango"):
        resolve_migration_credentials({MIGRATE_SECRET_VARIABLE: "m"}, fetch)


def test_fetch_errors_are_reported_by_type_only() -> None:
    def failing(secret_id: str) -> str:
        raise PermissionError(f"AccessDenied on {secret_id} with {MASTER_VALUE}")

    with pytest.raises(MigrationCredentialsError) as caught:
        resolve_migration_credentials(FIRST_DEPLOY, failing)
    assert str(caught.value) == "no se pudieron leer los secretos de la migración: PermissionError"
    assert caught.value.__cause__ is None


@given(value=st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=16))
def test_secret_values_never_appear_in_errors_or_repr(value: str) -> None:
    secrets = {"m": json.dumps({**MIGRATE_SECRET, "password": value, "port": "x"})}
    with pytest.raises(MigrationCredentialsError) as caught:
        resolve_migration_credentials({MIGRATE_SECRET_VARIABLE: "m"}, RecordingFetcher(secrets))
    assert value not in str(caught.value)
