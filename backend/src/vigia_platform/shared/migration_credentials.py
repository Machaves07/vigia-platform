"""Conexión y contraseñas de rol del trabajo de migración (TASK-106; infrastructure-design §5.4).

Dos orígenes, elegidos por el entorno:

- **AWS** (``VIGIA_DB_MIGRATE_SECRET`` presente, como en la tarea ``vigia-migrate`` de
  ``infra/stacks/compute.py``): las variables solo llevan nombres o ARN y los valores se leen de
  Secrets Manager con el rol de la tarea. El secreto ``db/migrate`` (formato de rotación de RDS:
  ``host``, ``port``, ``dbname``, ``username``, ``password``) da el destino. En el primer
  despliegue (``first_deploy=true``) llegan además ``VIGIA_DB_MASTER_SECRET_ARN`` (el secreto que
  gestiona RDS: se entra con el usuario maestro, que es quien puede crear los roles) y
  ``VIGIA_DB_APP_SECRET``; si no, se entra como ``vigia_migrate``.
- **Local** (sin esa variable; ``make migrate`` y las pruebas): la conexión sale de las variables
  de libpq (``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``, ``PGDATABASE``), que asyncpg lee
  por sí mismo, y las contraseñas de rol de ``VIGIA_DB_APP_PASSWORD`` y
  ``VIGIA_DB_MIGRATE_PASSWORD``.

En los dos, el modo TLS lo fijan ``PGSSLMODE`` y ``PGSSLROOTCERT`` (asyncpg los lee). Ningún
mensaje de error contiene un valor de secreto.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import URL

__all__ = [
    "APP_PASSWORD_VARIABLE",
    "APP_SECRET_VARIABLE",
    "MASTER_SECRET_VARIABLE",
    "MIGRATE_PASSWORD_VARIABLE",
    "MIGRATE_SECRET_VARIABLE",
    "MigrationCredentials",
    "MigrationCredentialsError",
    "resolve_migration_credentials",
]

MIGRATE_SECRET_VARIABLE: Final = "VIGIA_DB_MIGRATE_SECRET"  # noqa: S105 - nombre, no valor
"""Nombre del secreto ``vigia/<despliegue>/db/migrate``."""
MASTER_SECRET_VARIABLE: Final = "VIGIA_DB_MASTER_SECRET_ARN"  # noqa: S105 - nombre, no valor
"""ARN del secreto del usuario maestro que gestiona RDS (solo en el primer despliegue)."""
APP_SECRET_VARIABLE: Final = "VIGIA_DB_APP_SECRET"  # noqa: S105 - nombre, no valor
"""Nombre del secreto ``vigia/<despliegue>/db/app`` (solo en el primer despliegue)."""
APP_PASSWORD_VARIABLE: Final = "VIGIA_DB_APP_PASSWORD"  # noqa: S105 - nombre, no valor
"""Entorno local: contraseña de ``vigia_app``."""
MIGRATE_PASSWORD_VARIABLE: Final = "VIGIA_DB_MIGRATE_PASSWORD"  # noqa: S105 - nombre, no valor
"""Entorno local: contraseña de ``vigia_migrate``."""

APP_ROLE: Final = "vigia_app"
MIGRATE_ROLE: Final = "vigia_migrate"
_DRIVER: Final = "postgresql+asyncpg"

SecretFetcher = Callable[[str], str]
"""Devuelve el ``SecretString`` de un secreto por nombre o ARN."""


class MigrationCredentialsError(RuntimeError):
    """Falta un secreto o no tiene la forma esperada; el mensaje nunca incluye valores."""


@dataclass(frozen=True, slots=True)
class MigrationCredentials:
    """Destino de la migración y contraseñas de los roles que crea ``nuc_0001``."""

    url: URL = field(repr=False)
    role_passwords: Mapping[str, str] = field(repr=False)
    source: str
    """``aws`` o ``local``."""


def _secrets_manager_fetcher() -> SecretFetcher:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    client = boto3.client(
        "secretsmanager",
        config=Config(
            connect_timeout=5, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"}
        ),
    )

    def fetch(secret_id: str) -> str:
        value = client.get_secret_value(SecretId=secret_id)["SecretString"]
        if not isinstance(value, str):
            raise MigrationCredentialsError(f"el secreto {secret_id} no tiene SecretString")
        return value

    return fetch


def _secret_fields(
    fetch: SecretFetcher, secret_id: str, required: tuple[str, ...]
) -> dict[str, Any]:
    try:
        document = json.loads(fetch(secret_id))
    except ValueError:
        raise MigrationCredentialsError(f"el secreto {secret_id} no es un objeto JSON") from None
    if not isinstance(document, dict):
        raise MigrationCredentialsError(f"el secreto {secret_id} no es un objeto JSON")
    for key in required:
        value = document.get(key)
        valid = isinstance(value, str) and value != ""
        if key == "port":
            valid = (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, str) and value.isdigit()
            )
        if not valid:
            raise MigrationCredentialsError(f"al secreto {secret_id} le falta el campo {key!r}")
    return document


def _port(value: object) -> int:
    port = int(str(value))
    if not 0 < port < 65536:
        raise MigrationCredentialsError("puerto fuera de rango en el secreto db/migrate")
    return port


def _from_aws(environ: Mapping[str, str], fetch: SecretFetcher) -> MigrationCredentials:
    migrate_id = environ[MIGRATE_SECRET_VARIABLE]
    target = _secret_fields(fetch, migrate_id, ("host", "port", "dbname", "username", "password"))
    passwords = {MIGRATE_ROLE: str(target["password"])}
    app_id = environ.get(APP_SECRET_VARIABLE)
    if app_id:
        passwords[APP_ROLE] = str(_secret_fields(fetch, app_id, ("password",))["password"])
    master_id = environ.get(MASTER_SECRET_VARIABLE)
    login = _secret_fields(fetch, master_id, ("username", "password")) if master_id else target
    url = URL.create(
        _DRIVER,
        username=str(login["username"]),
        password=str(login["password"]),
        host=str(target["host"]),
        port=_port(target["port"]),
        database=str(target["dbname"]),
    )
    return MigrationCredentials(url=url, role_passwords=passwords, source="aws")


def _from_local(environ: Mapping[str, str]) -> MigrationCredentials:
    passwords = {
        role: environ[variable]
        for role, variable in (
            (APP_ROLE, APP_PASSWORD_VARIABLE),
            (MIGRATE_ROLE, MIGRATE_PASSWORD_VARIABLE),
        )
        if environ.get(variable)
    }
    # Sin host, usuario ni base: asyncpg los toma de las variables PG* del entorno.
    return MigrationCredentials(url=URL.create(_DRIVER), role_passwords=passwords, source="local")


def resolve_migration_credentials(
    environ: Mapping[str, str] | None = None, fetch: SecretFetcher | None = None
) -> MigrationCredentials:
    """Conexión y contraseñas de rol para ``alembic upgrade head`` según el entorno."""
    environ = os.environ if environ is None else environ
    if not environ.get(MIGRATE_SECRET_VARIABLE):
        return _from_local(environ)
    try:
        return _from_aws(environ, fetch or _secrets_manager_fetcher())
    except MigrationCredentialsError:
        raise
    except Exception as error:  # botocore: acceso denegado, red, secreto inexistente
        raise MigrationCredentialsError(
            f"no se pudieron leer los secretos de la migración: {type(error).__name__}"
        ) from None
