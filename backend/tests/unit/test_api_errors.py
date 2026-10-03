"""Tabla cerrada de errores, fallo cerrado por petición y etiquetas (TASK-133; LC-NUC-21).

- ``api_error_code`` tiene los once valores de §1 y los cuatro del pendiente nº 33, sin
  ``csrf_rejected``; cada uno con estado HTTP, mensaje en español y, solo los transitorios,
  ``retry_after_seconds`` y ``Retry-After``.
- Metapropiedad de NFR-NUC-51: ante cualquier excepción, la respuesta es un ``ApiErrorBody``
  estricto con el mensaje genérico del código y un ``correlation_id`` v7 generado; nunca lleva el
  texto de la excepción, una traza ni una ruta interna.
- Traducción de ``LedgerRejection``, ``ContextAbsent``, ``ExternalDependencyDown``, los fallos
  transitorios y los tiempos de espera; ``internal_error`` para lo demás.
- Reversión y liberación garantizadas: una ruta que falla dentro de ``db.transaction`` responde
  el error y su conexión vuelve al pool sin ``COMMIT``.
- ``labels.platform.es.json``: fallo cerrado ante un valor sin etiqueta; archivo mal formado o
  incompleto impide arrancar.
- ``Database.health``: organización inexistente fijada, una ida y vuelta, tope sin reintento.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.sql import Executable

from tests.api_support import World
from tests.factories import make_context
from tests.properties.test_db_retry import FakeConnection, FakePool, Journal, Phase
from tests.virtual_time import run_virtual
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.shared.api.app import (
    LABEL_BINDINGS,
    UnitRegistration,
    platform_permissions,
    platform_units,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import (
    HTTP_STATUS,
    TRANSIENT_CODES,
    ApiError,
    ApiErrorBody,
    ApiErrorCode,
    ApiStartupError,
    ExternalDependencyDown,
    from_ledger_rejection,
    translate,
)
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH, MissingLabel, PlatformLabels
from vigia_platform.shared.context import ContextAbsent, Role
from vigia_platform.shared.db import (
    HEALTH_ORGANIZATION_ID,
    ChainLockedTimeout,
    Database,
    PoolClass,
    ProcessKind,
    TemporarilyUnavailable,
)
from vigia_platform.shared.secrets import Dependency, SecretsUnavailable
from vigia_platform.shared.storage import StorageUnavailable

KNOWN = platform_permissions() | {"ledger.read"}
"""La matriz (las rutas de ``platform_units()`` exigen sus claves) y la clave de prueba."""
LABELS = PlatformLabels.load()
INTERNAL_DETAIL = "SELECT hash FROM identity.user_account -- /srv/vigia/app.py:42 eyJhbGciOi"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
"""Lo que exige la barrera anti-falsificación (TASK-134) a un método que cambia estado."""


class AllowAll:
    async def authorize(self, request: Any, permission: str) -> None:
        return None


def _app(router: APIRouter, detail_codes: tuple[str, ...] = ()) -> Any:
    unit = UnitRegistration("prueba", routers=(router,), detail_codes=detail_codes)
    return World().app(
        units=(*platform_units(), unit),
        permissions=KNOWN,
        runtime={"authorizer": AllowAll()},
    )


def _raising(error: BaseException, detail_codes: tuple[str, ...] = ()) -> Any:
    router = APIRouter()

    async def fails() -> None:
        raise error

    router.add_api_route("/falla", fails, methods=["GET"], dependencies=[requires("ledger.read")])
    return _app(router, detail_codes)


def _call(app: Any) -> Any:
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.get("/falla")


def _body(response: Any) -> ApiErrorBody:
    body = ApiErrorBody.model_validate_json(response.content)
    assert set(response.json()) <= set(ApiErrorBody.model_fields)
    assert body.correlation_id.version == 7
    assert body.message_es == LABELS.label("api_error_code", body.code.value)
    assert response.status_code == HTTP_STATUS[body.code]
    return body


# --- Tabla cerrada -----------------------------------------------------------------------------


def test_api_error_code_is_the_closed_list_of_pending_33() -> None:
    assert {code.value for code in ApiErrorCode} == {
        "invalid_request",
        "not_found",
        "unauthenticated",
        "second_factor_required",
        "forbidden",
        "throttled",
        "rate_limited",
        "payload_too_large",
        "conflict",
        "temporarily_unavailable",
        "internal_error",
        "privacy_notice_required",
        "period_too_long",
        "zone_without_node",
        "storage_unavailable",
    }
    assert "csrf_rejected" not in {code.value for code in ApiErrorCode}
    assert set(HTTP_STATUS) == set(ApiErrorCode)
    assert HTTP_STATUS[ApiErrorCode.NOT_FOUND] == 404
    assert set(ApiErrorCode) >= TRANSIENT_CODES


@pytest.mark.parametrize("code", list(ApiErrorCode))
def test_every_code_answers_with_its_status_and_message(code: ApiErrorCode) -> None:
    response = _call(_raising(ApiError(code)))
    body = _body(response)
    assert body.code is code
    if code in TRANSIENT_CODES:
        assert body.retry_after_seconds == 5 and response.headers["retry-after"] == "5"
    else:
        assert body.retry_after_seconds is None and "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_api_error_rejects_inconsistent_values() -> None:
    with pytest.raises(ValueError, match="no es transitorio"):
        ApiError(ApiErrorCode.CONFLICT, retry_after_seconds=5)
    for retry in (0, -1, 3_601, cast(int, 1.5), cast(int, True)):
        with pytest.raises(ValueError):
            ApiError(ApiErrorCode.RATE_LIMITED, retry_after_seconds=retry)
    with pytest.raises(ValueError):
        ApiError(ApiErrorCode.CONFLICT, detail_code="sin prefijo")
    with pytest.raises(TypeError):
        ApiError(cast(ApiErrorCode, "not_found"))


def test_retry_after_travels_in_body_and_header() -> None:
    response = _call(_raising(ApiError(ApiErrorCode.RATE_LIMITED, retry_after_seconds=42)))
    assert _body(response).retry_after_seconds == 42
    assert response.headers["retry-after"] == "42"


# --- detail_code -------------------------------------------------------------------------------


def test_a_registered_detail_code_travels_next_to_the_code() -> None:
    error = ApiError(ApiErrorCode.CONFLICT, detail_code="loop_closure_already_signed")
    body = _body(_call(_raising(error, ("loop_closure_already_signed",))))
    assert body.code is ApiErrorCode.CONFLICT
    assert body.detail_code == "loop_closure_already_signed"


def test_an_unregistered_detail_code_at_runtime_is_an_internal_error() -> None:
    error = ApiError(ApiErrorCode.CONFLICT, detail_code="loop_not_registered")
    body = _body(_call(_raising(error)))
    assert body.code is ApiErrorCode.INTERNAL_ERROR and body.detail_code is None


# --- Traducción --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "retry"),
    [
        (ContextAbsent(), ApiErrorCode.INTERNAL_ERROR, None),
        (ExternalDependencyDown("mail", retry_after_seconds=30), "temporarily_unavailable", 30),
        (TemporarilyUnavailable(retry_after_seconds=7), "temporarily_unavailable", 7),
        (TemporarilyUnavailable(commit_outcome_unknown=True), "temporarily_unavailable", 5),
        (ChainLockedTimeout(), "temporarily_unavailable", 5),
        (SecretsUnavailable(Dependency.KMS, "decrypt"), "temporarily_unavailable", 5),
        (StorageUnavailable("head_object"), "storage_unavailable", 5),
        (TimeoutError(), "temporarily_unavailable", 5),
        (RuntimeError(INTERNAL_DETAIL), "internal_error", None),
        (KeyError(INTERNAL_DETAIL), "internal_error", None),
        (RecursionError(), "internal_error", None),
        (ValueError(INTERNAL_DETAIL), "internal_error", None),
    ],
)
def test_exceptions_translate_to_generic_codes(
    error: Exception, code: str, retry: int | None
) -> None:
    translated = translate(error)
    assert translated.code == code and translated.retry_after_seconds == retry
    response = _call(_raising(error))
    body = _body(response)
    assert body.code == code and body.retry_after_seconds == retry
    assert "SELECT" not in response.text and "app.py" not in response.text


def test_every_ledger_rejection_has_a_translation() -> None:
    expected = {
        LedgerRejectionCode.CONTEXT_ABSENT: ApiErrorCode.INTERNAL_ERROR,
        LedgerRejectionCode.RECORD_TYPE_UNKNOWN: ApiErrorCode.INTERNAL_ERROR,
        LedgerRejectionCode.CONTENT_INVALID: ApiErrorCode.INVALID_REQUEST,
        LedgerRejectionCode.FREE_TEXT_REJECTED: ApiErrorCode.INVALID_REQUEST,
        LedgerRejectionCode.IDEMPOTENCY_CONFLICT: ApiErrorCode.CONFLICT,
        LedgerRejectionCode.EVIDENCE_MISSING: ApiErrorCode.INVALID_REQUEST,
        LedgerRejectionCode.EVIDENCE_HASH_MISMATCH: ApiErrorCode.INVALID_REQUEST,
        LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED: ApiErrorCode.INVALID_REQUEST,
        LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT: ApiErrorCode.TEMPORARILY_UNAVAILABLE,
    }
    assert set(expected) == set(LedgerRejectionCode)
    for rejection_code, api_code in expected.items():
        rejection = LedgerRejection.of(rejection_code, "/cameras/0/clips/0/sha256")
        error = from_ledger_rejection(rejection)
        assert error.code is api_code and error.detail_code is None
    with pytest.raises(TypeError):
        from_ledger_rejection(cast(LedgerRejection, "content_invalid"))


def test_unknown_paths_methods_and_validation_are_generic() -> None:
    router = APIRouter()

    async def typed(limit: int) -> dict[str, int]:
        return {"limit": limit}

    router.add_api_route("/falla", typed, methods=["GET"], dependencies=[requires("ledger.read")])
    with TestClient(_app(router), raise_server_exceptions=False) as client:
        missing = client.get("/no-existe/../../etc/passwd")
        method = client.post("/health/live", headers=SAME_ORIGIN)
        invalid = client.get("/falla", params={"limit": "diez"})
        valid = client.get("/falla", params={"limit": "10"})
    assert _body(missing).code is ApiErrorCode.NOT_FOUND
    assert _body(method).code is ApiErrorCode.NOT_FOUND
    assert _body(invalid).code is ApiErrorCode.INVALID_REQUEST
    assert "diez" not in invalid.text and "limit" not in invalid.text
    assert valid.json() == {"limit": 10}


def test_the_correlation_id_is_never_taken_from_the_client() -> None:
    sent = str(uuid.uuid4())
    with TestClient(_raising(RuntimeError()), raise_server_exceptions=False) as client:
        response = client.get("/falla", headers={"X-Correlation-Id": sent})
    assert str(_body(response).correlation_id) != sent


EXCEPTIONS = st.sampled_from(
    [RuntimeError, ValueError, KeyError, TypeError, ZeroDivisionError, OSError, LookupError]
)


@given(kind=EXCEPTIONS, message=st.text(min_size=1, max_size=200))
def test_metaproperty_any_exception_yields_a_strict_generic_body(
    kind: type[Exception], message: str
) -> None:
    response = _call(_raising(kind(message)))
    body = _body(response)
    assert body.code is ApiErrorCode.INTERNAL_ERROR
    assert json.loads(response.text) == body.model_dump(mode="json", exclude_none=True)
    if len(message.strip()) > 3:
        assert message not in body.message_es
    assert "Traceback" not in response.text


# --- Reversión y liberación ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [RuntimeError(INTERNAL_DETAIL), ApiError(ApiErrorCode.CONFLICT), asyncio.CancelledError()],
)
def test_a_failing_route_rolls_back_and_releases_its_connection(error: BaseException) -> None:
    journal = Journal()
    pool = FakePool(journal)
    database = Database(
        {PoolClass.NODE: pool, PoolClass.PERSON: pool},
        process=ProcessKind.API,
        attempt_timeout_seconds=5.0,
        command_timeout_seconds=5.0,
    )
    context = make_context()
    router = APIRouter()

    async def writes() -> None:
        async with database.transaction(context) as transaction:
            await transaction.execute(text("INSERT INTO ledger.x VALUES (1)"))
            raise error

    router.add_api_route("/falla", writes, methods=["POST"], dependencies=[requires("ledger.read")])
    with TestClient(_app(router), raise_server_exceptions=False) as client:
        response = client.post("/falla", headers=SAME_ORIGIN)
    assert response.status_code in (409, 500)
    assert journal.commits_sent == 0 and journal.effects_applied == 0
    assert journal.released + journal.discarded == 1


# --- Etiquetas ---------------------------------------------------------------------------------


DESIGN_ENUMERATIONS = {
    "role",
    "scope_level",
    "organization_kind",
    "organization_status",
    "account_status",
    "session_status",
    "credential_kind",
    "invitation_status",
    "throttle_subject_kind",
    "concession_status",
    "actor_kind",
    "context_origin",
    "signing_purpose",
    "key_status",
    "chain_level",
    "checkpoint_kind",
    "audit_outcome",
    "coverage_state",
    "coverage_layer",
    "platform_cause",
    "communication_state",
    "delivery_status",
    "circuit_state",
    "verification_result",
    "api_error_code",
}
"""Las enumeraciones con etiqueta en español de ``domain-entities.md`` §1."""


def test_the_labels_file_covers_every_labelled_enumeration() -> None:
    assert LABELS.enumerations() == DESIGN_ENUMERATIONS
    assert LABELS.require_complete(LABEL_BINDINGS) == []
    assert LABELS.label("role", Role.COPASST) == "COPASST"


@pytest.mark.parametrize(
    ("enumeration", "value"),
    [("role", "superuser"), ("role", ""), ("no_such_enum", "x"), ("role", cast(str, 3))],
)
def test_a_value_without_label_fails_closed(enumeration: str, value: str) -> None:
    with pytest.raises(MissingLabel):
        LABELS.label(enumeration, value)


def _labels_file(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(
        document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d["role"].pop("copasst"), id="falta-un-rol"),
        pytest.param(lambda d: d.pop("api_error_code"), id="faltan-mensajes"),
        pytest.param(lambda d: d["role"].update(copasst=""), id="etiqueta-vacia"),
        pytest.param(lambda d: d["role"].update(copasst="a\u0000b"), id="control"),
        pytest.param(lambda d: d["role"].update(copasst="x" * 257), id="larga"),
        pytest.param(lambda d: d.update(Role={"a": "b"}), id="nombre-no-valido"),
        pytest.param(lambda d: d["api_error_code"].update(conflict="​"), id="solo-invisible"),
    ],
)
def test_an_incomplete_or_malformed_labels_file_prevents_startup(
    tmp_path: Path, mutate: Any
) -> None:
    document = json.loads(DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
    mutate(document)
    with pytest.raises(ApiStartupError):
        World().app(labels_path=_labels_file(tmp_path, document))


@pytest.mark.parametrize(
    "raw",
    ['{"role": {"a": "b", "a": "c"}}', "[]", "{", "\udcff", '{"role": []}'],
)
def test_labels_file_parsing_fails_closed(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "labels.json"
    path.write_bytes(raw.encode("utf-8", "surrogateescape"))
    with pytest.raises(ApiStartupError):
        World().app(labels_path=path)


def test_a_missing_labels_file_prevents_startup(tmp_path: Path) -> None:
    with pytest.raises(ApiStartupError, match="no se pudo leer"):
        World().app(labels_path=tmp_path / "no-existe.json")


# --- Database.health ---------------------------------------------------------------------------


class _HealthConnection(FakeConnection):
    def __init__(self, journal: Journal, row: tuple[object, object], **kwargs: Any) -> None:
        super().__init__(journal, None, **kwargs)
        self.row = row

    async def execute(self, statement: Executable, parameters: Any) -> Result[Any]:
        result = await super().execute(statement, parameters)
        if "set_config" in str(statement):
            return result
        row = self.row

        class _Row:
            def all(self) -> list[tuple[object, object]]:
                return [row]

        return cast(Result[Any], _Row())


class _HealthPool(FakePool):
    def __init__(self, journal: Journal, row: tuple[object, object], **kwargs: Any) -> None:
        super().__init__(journal)
        self.row = row
        self.kwargs = kwargs

    async def acquire(self) -> Any:
        self.journal.attempts += 1
        return _HealthConnection(self.journal, self.row, **self.kwargs)


def _health_database(pool: FakePool) -> Database:
    return Database(
        {PoolClass.NODE: pool, PoolClass.PERSON: pool},
        process=ProcessKind.API,
        attempt_timeout_seconds=5.0,
        command_timeout_seconds=5.0,
    )


HEALTH_CAP_SECONDS = 5.0
"""Tope externo de cada escenario de ``health``. Corren en tiempo virtual (VIG-134): el tope de
``health`` no vence por un runner cargado, solo si el pool falso deja de responder."""


def test_database_health_sets_a_nonexistent_organization_and_reads_once() -> None:
    journal = Journal()
    database = _health_database(_HealthPool(journal, (0, 4)))
    health = run_virtual(database.health(timeout_seconds=1.0), cap_seconds=HEALTH_CAP_SECONDS)
    assert health.visible_organizations == 0 and health.schema_version == 4
    assert journal.scopes == [
        {
            "organization_id": str(HEALTH_ORGANIZATION_ID),
            "actor_kind": "system",
            "concession_id": "",
        }
    ]
    assert journal.begins == [True] and journal.released == 1 and journal.attempts == 1


def test_database_health_gives_up_at_its_timeout_without_retrying() -> None:
    journal = Journal()
    database = _health_database(_HealthPool(journal, (0, 4), hang_at=Phase.EXECUTE))

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TemporarilyUnavailable):
            await database.health(timeout_seconds=0.2)
        elapsed = loop.time() - start
        await database.dispose()
        return elapsed

    assert run_virtual(scenario(), cap_seconds=HEALTH_CAP_SECONDS) < 0.2 + 0.5
    assert journal.attempts == 1 and journal.terminated >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0.0, -1.0, 5.1])
async def test_database_health_timeout_must_fit_the_attempt(timeout: float) -> None:
    database = _health_database(_HealthPool(Journal(), (0, 1)))
    with pytest.raises(ValueError):
        await database.health(timeout_seconds=timeout)


@pytest.mark.parametrize("row", [("0", 1), (0, "1"), (True, 1)])
def test_database_health_rejects_unexpected_types(row: tuple[object, object]) -> None:
    database = _health_database(_HealthPool(Journal(), row))
    with pytest.raises(TypeError):
        run_virtual(database.health(timeout_seconds=1.0), cap_seconds=HEALTH_CAP_SECONDS)
