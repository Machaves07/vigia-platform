"""Configuración de la raíz de composición (A-52; LC-NUC-19; §5.3 de U-02 y §6 de U-03).

``RuntimeConfig`` es el modelo estricto e inmutable que los tres constructores leen **una vez**
del entorno (``RuntimeConfig.from_environ``). Las variables solo llevan nombres, ARN, tamaños y
tiempos: ninguna contraseña ni clave llega por el entorno (§5.3). La credencial de ``vigia_app``
se lee del secreto que nombra ``VIGIA_DB_APP_SECRET`` (``shared.runtime.db_credentials``).

Variables (``infra/stacks/compute.py``; las de tamaño, pendiente nº 17 de U-03):

- obligatorias en los tres procesos: ``VIGIA_ENVIRONMENT``, ``AWS_REGION``,
  ``VIGIA_DB_APP_SECRET``, ``VIGIA_SIGNING_SECRET_PREFIX`` (``vigia/<entorno>/signing/``) y
  ``VIGIA_SECRETS_KEY_ARN``;
- obligatorias según el proceso (``RuntimeConfig.require``): ``VIGIA_PROVIDER_ORGANIZATION_ID``
  y ``VIGIA_EVIDENCE_BUCKET`` en ``vigia-api`` y ``vigia-worker``; ``VIGIA_ARCHIVE_BUCKET`` en
  ``vigia-worker``;
- opcionales: ``VIGIA_NODE_CA_KEY_ARN``, ``VIGIA_EDGE_BUCKET``, ``VIGIA_CRL_KEY``,
  ``VIGIA_NODE_TRUST_STORE_ARN``; ``PGSSLMODE`` y ``PGSSLROOTCERT`` (TLS de la base, como en
  ``vigia-migrate``; por defecto ``verify-full``); ``VIGIA_BREACH_LIST_PATH`` (respaldo local de
  contraseñas filtradas de ``vigia-api``; por defecto ``resources/pwned-top100k.txt``);
- tamaños, con el valor del diseño por defecto: ``VIGIA_DB_POOL_NODE`` (10),
  ``VIGIA_DB_POOL_PERSON`` (5), ``VIGIA_DB_MAX_OVERFLOW`` (0: el único admitido),
  ``VIGIA_DB_POOL_TIMEOUT_SECONDS`` (5), ``VIGIA_DB_STATEMENT_TIMEOUT_MS`` (por defecto el del
  proceso: 10 000 en la API y 30 000 en el worker), ``VIGIA_THREADPOOL_SIZE`` (4),
  ``VIGIA_BULKHEAD_NODE`` (35), ``VIGIA_BULKHEAD_PERSON`` (15) y ``VIGIA_UVICORN_WORKERS`` (2);
  ``VIGIA_BULKHEAD_PERSON`` por debajo del 30 % de la suma de los dos mamparos impide arrancar
  (NFR-GOB-19, ``shared.bulkheads``);
- documentos firmados (LC-GOB-05, §6 de U-03): ``VIGIA_DOCUMENTS_PREFIX`` (``documents/``, el
  único que admite la restricción ``document_upload_grant_storage_key_format`` de ``gob_0017``:
  otro prefijo exige una migración) y ``VIGIA_DOCUMENTS_MAX_BYTES`` (de 1 a 20 971 520);
- alta del nodo (TASK-219): ``VIGIA_NODES_BASE_URL`` (``https://nodes.<dominio>/api/nodes``, la
  que se entrega al nodo) y ``VIGIA_ENROLLMENT_SOURCE_KEY_SECRET`` (el secreto de la clave estable
  del hash de origen de los intentos), opcionales: sin ellas el alta responde transitorio;
- ``VIGIA_AWS_ENDPOINT_URL``: punto de conexión único de S3, KMS y Secrets Manager. **Solo** en
  ``local`` y ``test`` (LocalStack); en cualquier otro entorno detiene el arranque.

Un valor ausente o no válido produce ``RuntimeConfigInvalid``: el mensaje, en español, nombra la
variable y **nunca** su valor (ERR-019). Por eso cada variable se valida aquí antes de llegar a
Pydantic, cuyos mensajes sí repetirían el valor recibido.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Self, TextIO

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from vigia_platform.shared.bulkheads import (
    PERSON_RESERVE_PERCENT,
    PERSON_VARIABLE,
    BulkheadSettings,
    reserve_problem,
)
from vigia_platform.shared.db import SslMode

__all__ = [
    "ENDPOINT_ENVIRONMENTS",
    "KNOWN_VARIABLES",
    "VARIABLES",
    "RuntimeConfig",
    "RuntimeConfigInvalid",
    "report_invalid",
]

ENDPOINT_ENVIRONMENTS: Final = frozenset({"local", "test"})
"""Entornos que admiten ``VIGIA_AWS_ENDPOINT_URL`` (LocalStack)."""

_ENVIRONMENT: Final = re.compile(r"(local|test|pilot|staging-[1-9][0-9]{0,2})")
_REGION: Final = re.compile(r"[a-z]{2}(-[a-z]+)+-[1-9]")
_SECRET_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_+=.@-]{0,511}")
_SIGNING_PREFIX: Final = re.compile(r"vigia/([a-z0-9][a-z0-9-]{0,62})/signing/")
_KMS_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_-]{0,2047}")
_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_OBJECT_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9!_.*'()/=-]{0,511}")
_ARN: Final = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:[A-Za-z0-9:/_.-]{1,1024}")
_ENDPOINT: Final = re.compile(r"https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")
_PATH: Final = re.compile(r"/[A-Za-z0-9/_.-]{1,1023}")
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DIGITS: Final = re.compile(r"[0-9]{1,9}")
_DOCUMENTS_PREFIX: Final = re.compile(r"documents/")
_NODES_BASE_URL: Final = re.compile(
    r"https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+){0,16}/?"
)
"""``Endpoints.ingest_base_url`` del contrato: ``https``, sin consulta ni fragmento."""
_SECRET_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_+=.@-]{0,2047}")


class RuntimeConfigInvalid(ValueError):
    """Variable de entorno ausente o no válida. Nombra la variable, nunca su valor (ERR-019)."""

    def __init__(self, variable: str, reason: str = "ausente o no válida") -> None:
        super().__init__(f"configuración no válida: {variable} {reason}")
        self.variable = variable


def report_invalid(error: BaseException, stream: TextIO | None = None) -> bool:
    """Escribe en la salida de errores la línea ``{"error": "config_invalid", "variable": …}``.

    Los registros estructurados solo llevan mensajes constantes y valores de listas cerradas
    cortas (``shared.observability``), así que no pueden nombrar una variable larga: esta línea
    es la que la nombra, como ``vigia-admin`` con su ``config_invalid``. Solo sale el nombre de una
    variable conocida y el motivo fijo del código; nunca un valor. ``False`` si ``error`` no es
    ``RuntimeConfigInvalid``.
    """
    if not isinstance(error, RuntimeConfigInvalid):
        return False
    variable = error.variable if error.variable in KNOWN_VARIABLES else "?"
    line = {"error": "config_invalid", "variable": variable, "mensaje": str(error)}
    target = stream if stream is not None else sys.stderr
    target.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    target.flush()
    return True


# --- Lectores ------------------------------------------------------------------------------------


def _text(pattern: re.Pattern[str]) -> Callable[[str], object]:
    def parse(value: str) -> object:
        if pattern.fullmatch(value) is None:
            raise ValueError
        return value

    return parse


def _integer(minimum: int, maximum: int) -> Callable[[str], object]:
    def parse(value: str) -> object:
        if _DIGITS.fullmatch(value) is None:
            raise ValueError
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError
        return number

    return parse


def _organization(value: str) -> object:
    if _UUID.fullmatch(value) is None:
        raise ValueError
    return uuid.UUID(value)


def _sslmode(value: str) -> object:
    return SslMode(value)


@dataclass(frozen=True, slots=True)
class _Variable:
    field: str
    name: str
    parse: Callable[[str], object]
    required: bool = False


VARIABLES: Final[tuple[_Variable, ...]] = (
    _Variable("environment", "VIGIA_ENVIRONMENT", _text(_ENVIRONMENT), required=True),
    _Variable("aws_region", "AWS_REGION", _text(_REGION), required=True),
    _Variable("aws_endpoint_url", "VIGIA_AWS_ENDPOINT_URL", _text(_ENDPOINT)),
    _Variable("provider_organization_id", "VIGIA_PROVIDER_ORGANIZATION_ID", _organization),
    _Variable("db_app_secret", "VIGIA_DB_APP_SECRET", _text(_SECRET_NAME), required=True),
    _Variable("db_sslmode", "PGSSLMODE", _sslmode),
    _Variable("db_ssl_root_cert", "PGSSLROOTCERT", _text(_PATH)),
    _Variable(
        "signing_secret_prefix",
        "VIGIA_SIGNING_SECRET_PREFIX",
        _text(_SIGNING_PREFIX),
        required=True,
    ),
    _Variable("secrets_key_arn", "VIGIA_SECRETS_KEY_ARN", _text(_KMS_KEY_ID), required=True),
    _Variable("node_ca_key_arn", "VIGIA_NODE_CA_KEY_ARN", _text(_KMS_KEY_ID)),
    _Variable("evidence_bucket", "VIGIA_EVIDENCE_BUCKET", _text(_BUCKET)),
    _Variable("archive_bucket", "VIGIA_ARCHIVE_BUCKET", _text(_BUCKET)),
    _Variable("edge_bucket", "VIGIA_EDGE_BUCKET", _text(_BUCKET)),
    _Variable("crl_key", "VIGIA_CRL_KEY", _text(_OBJECT_KEY)),
    _Variable("node_trust_store_arn", "VIGIA_NODE_TRUST_STORE_ARN", _text(_ARN)),
    _Variable("breach_list_path", "VIGIA_BREACH_LIST_PATH", _text(_PATH)),
    _Variable("db_pool_node", "VIGIA_DB_POOL_NODE", _integer(1, 100)),
    _Variable("db_pool_person", "VIGIA_DB_POOL_PERSON", _integer(1, 100)),
    _Variable("db_max_overflow", "VIGIA_DB_MAX_OVERFLOW", _integer(0, 0)),
    _Variable("db_pool_timeout_seconds", "VIGIA_DB_POOL_TIMEOUT_SECONDS", _integer(1, 60)),
    _Variable("db_statement_timeout_ms", "VIGIA_DB_STATEMENT_TIMEOUT_MS", _integer(100, 600_000)),
    _Variable("threadpool_size", "VIGIA_THREADPOOL_SIZE", _integer(1, 64)),
    _Variable("bulkhead_node", "VIGIA_BULKHEAD_NODE", _integer(1, 1_000)),
    _Variable("bulkhead_person", "VIGIA_BULKHEAD_PERSON", _integer(1, 1_000)),
    _Variable("uvicorn_workers", "VIGIA_UVICORN_WORKERS", _integer(1, 16)),
    _Variable("documents_prefix", "VIGIA_DOCUMENTS_PREFIX", _text(_DOCUMENTS_PREFIX)),
    _Variable("documents_max_bytes", "VIGIA_DOCUMENTS_MAX_BYTES", _integer(1, 20_971_520)),
    _Variable("nodes_base_url", "VIGIA_NODES_BASE_URL", _text(_NODES_BASE_URL)),
    _Variable(
        "enrollment_source_key_secret",
        "VIGIA_ENROLLMENT_SOURCE_KEY_SECRET",
        _text(_SECRET_ID),
    ),
)
"""Cada campo de ``RuntimeConfig`` con su variable y su lector."""

_VARIABLE_OF: Final = {variable.field: variable.name for variable in VARIABLES}
KNOWN_VARIABLES: Final = frozenset(
    {*_VARIABLE_OF.values(), "VIGIA_PUBLIC_ORIGIN", "VIGIA_EVIDENCE_BUCKET", "VIGIA_ARCHIVE_BUCKET"}
)
"""Las variables que ``RuntimeConfigInvalid`` puede nombrar (lista cerrada)."""


class RuntimeConfig(BaseModel):
    """Configuración común de ``vigia-api``, ``vigia-worker`` y ``vigia-admin``."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    environment: str
    aws_region: str
    aws_endpoint_url: str | None = None
    provider_organization_id: uuid.UUID | None = None
    db_app_secret: str
    """Nombre del secreto ``vigia/<despliegue>/db/app``; nunca su valor."""
    db_sslmode: SslMode = SslMode.VERIFY_FULL
    db_ssl_root_cert: str | None = None
    signing_secret_prefix: str
    secrets_key_arn: str
    node_ca_key_arn: str | None = None
    evidence_bucket: str | None = None
    archive_bucket: str | None = None
    edge_bucket: str | None = None
    crl_key: str | None = None
    node_trust_store_arn: str | None = None
    breach_list_path: str | None = None
    """Respaldo local de contraseñas filtradas; por defecto ``resources/pwned-top100k.txt``."""
    db_pool_node: int = 10
    db_pool_person: int = 5
    db_max_overflow: int = 0
    db_pool_timeout_seconds: int = 5
    db_statement_timeout_ms: int | None = None
    threadpool_size: int = 4
    bulkhead_node: int = 35
    bulkhead_person: int = 15
    uvicorn_workers: int = 2
    documents_prefix: str = "documents/"
    documents_max_bytes: int = 20_971_520
    nodes_base_url: str | None = None
    """``initial_configuration.endpoints.ingest_base_url`` del alta (``https://nodes.<dominio>``
    ``/api/nodes``); sin ella, el alta responde transitorio."""
    enrollment_source_key_secret: str | None = None
    """ARN o nombre del secreto con la clave estable del hash de origen del alta (≥ 32 bytes, la
    misma en todas las instancias); sin ella, el alta falla cerrada (TASK-218, TASK-219)."""

    @model_validator(mode="after")
    def _person_reserve(self) -> Self:
        problem = reserve_problem(self.bulkhead_node, self.bulkhead_person)
        if problem is not None:
            raise ValueError(problem)
        return self

    @property
    def bulkheads(self) -> BulkheadSettings:
        """Los tamaños de los semáforos por clase de ruta (LC-GOB-20)."""
        return BulkheadSettings(node=self.bulkhead_node, person=self.bulkhead_person)

    @property
    def signing_environment(self) -> str:
        """El ``<entorno>`` de ``vigia/<entorno>/signing/`` (nombre de los secretos de firma)."""
        match = _SIGNING_PREFIX.fullmatch(self.signing_secret_prefix)
        if match is None:  # pragma: no cover - ``from_environ`` ya lo validó
            raise RuntimeConfigInvalid("VIGIA_SIGNING_SECRET_PREFIX")
        return match.group(1)

    def require(self, field: str) -> Any:
        """El valor de ``field``; ``RuntimeConfigInvalid`` con su variable si este proceso lo
        exige y no está."""
        value = getattr(self, field)
        if value is None:
            raise RuntimeConfigInvalid(_VARIABLE_OF[field], "ausente: este proceso la exige")
        return value

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> RuntimeConfig:
        """Lee y valida cada variable; la primera que falte o no valga detiene la lectura."""
        values: dict[str, object] = {}
        for variable in VARIABLES:
            raw = environ.get(variable.name)
            if raw is None or raw == "":
                if variable.required:
                    raise RuntimeConfigInvalid(variable.name, "ausente")
                continue
            try:
                values[variable.field] = variable.parse(raw)
            except ValueError:
                raise RuntimeConfigInvalid(variable.name) from None
        environment = values["environment"]
        if "aws_endpoint_url" in values and environment not in ENDPOINT_ENVIRONMENTS:
            raise RuntimeConfigInvalid(
                "VIGIA_AWS_ENDPOINT_URL", "solo se admite en los entornos local y test"
            )
        fields = cls.model_fields
        node = values.get("bulkhead_node", fields["bulkhead_node"].default)
        person = values.get("bulkhead_person", fields["bulkhead_person"].default)
        if isinstance(node, int) and isinstance(person, int) and reserve_problem(node, person):
            raise RuntimeConfigInvalid(
                PERSON_VARIABLE,
                f"deja a las personas menos del {PERSON_RESERVE_PERCENT} % de los puestos del "
                "mamparo (NFR-GOB-19)",
            )
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            # El mensaje de Pydantic repetiría el valor: solo se conserva qué campo falló.
            location = error.errors()[0]["loc"]
            field = str(location[0]) if location else "?"
            raise RuntimeConfigInvalid(_VARIABLE_OF.get(field, field)) from None
