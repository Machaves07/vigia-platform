"""Traducción única a ``rejection_code`` con el estado de A-37 (TASK-206; LC-GOB-19 punto 4).

- **Tabla**: para cada operación del contrato y cada código que su regla permite (``OPERATIONS``),
  ``status_of`` da el estado declarado, que es el de A-37 (``schema_invalid``: 400 si el cuerpo
  no se interpreta o la cabecera es inválida, 422 si incumple el esquema); ``payload_too_large`` en
  las operaciones sin cuerpo sale con el 413 de A-37; un código que la operación no admite sale
  como ``temporarily_unavailable``.
- ``rejected_newer`` nunca es un ``code`` y ``timestamp_out_of_window`` es permanente.
- **Metapropiedad**: cualquier fallo (de la cadena compartida, de la verificación previa, del
  expediente, de un dominio de U-03 o desconocido, con mensajes que nombran dominios) en cualquier
  operación sale como ``404`` sin cuerpo o como un ``RejectionResponse`` que el modelo estricto de
  U-01 acepta, con un código que la operación admite, su estado, ``retryable`` según la clase del
  código, ``retry_after_seconds`` de 1 a 60 en los transitorios y un ``message_es`` de la tabla
  genérica; nunca un ``ApiError`` ni un nombre o mensaje de dominio. Se comprueba sobre la tabla
  (``render``) y sobre la ruta de prueba interna (HTTP, con las cabeceras de nodo).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from starlette.exceptions import HTTPException
from vigia_contracts.models.api import ContractValidationError, parse_rejection_response
from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.server_skeleton import OPERATIONS

from tests.node_api_support import VERSION, node_world, zone_of
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.identity.authz.context import NodeContextReason, NodeContextRejected
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.node_api.rejections import (
    A37_STATUS,
    MESSAGES,
    TRANSIENT_CODES,
    LedgerRejected,
    NodeRejection,
    NotFound,
    body_for,
    render,
    status_of,
    translate,
)
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.bulkheads import BulkheadSaturated
from vigia_platform.shared.db import RouteClass, TemporarilyUnavailable
from vigia_platform.shared.observability.redaction import DEFAULT_ENUMERATIONS
from vigia_platform.shared.storage import StorageUnavailable

DOMAIN_WORDS = (
    "catalog",
    "Catalog",
    "fleet",
    "Fleet",
    "ledger",
    "Ledger",
    "Escritor",
    "Traceback",
    "admission",
    "secreto-de-dominio",
)
"""Lo que nunca puede aparecer en una respuesta a un nodo (nombres y mensajes de U-03)."""


def _declared(route: NodeRoute) -> dict[str, tuple[int, ...]]:
    operation = OPERATIONS[route.operation_id]
    found: dict[str, list[int]] = {}
    for response in operation.responses:
        for code in response.rejection_codes or ():
            found.setdefault(code, []).append(response.status_code)
    return {code: tuple(statuses) for code, statuses in found.items()}


# --- Tabla A-37 por operación --------------------------------------------------------------------


@pytest.mark.parametrize("route", list(NodeRoute))
def test_every_code_of_every_operation_has_its_a37_status(route: NodeRoute) -> None:
    for code, statuses in _declared(route).items():
        rejection_code = RejectionCode(code)
        if rejection_code is RejectionCode.SCHEMA_INVALID and len(statuses) > 1:
            assert status_of(route, NodeRejection(rejection_code, body_level=True)) == 400
            assert status_of(route, NodeRejection(rejection_code)) == 422
            continue
        (status,) = statuses
        assert status_of(route, NodeRejection(rejection_code)) == status, (route, code)
        if rejection_code is RejectionCode.SCHEMA_INVALID:
            # A-37 lo pone en 400 (cabecera o cuerpo ilegible) y en 422 (esquema).
            assert status in (400, 422), (route, code)
        else:
            assert A37_STATUS[rejection_code] == status, (route, code)


def test_every_mutual_tls_operation_answers_node_zone_mismatch_with_403() -> None:
    for route in NodeRoute:
        declared = _declared(route)
        if route.mutual_tls:
            assert declared["node_zone_mismatch"] == (403,), route
            assert declared["node_revoked"] == declared["node_not_enrolled"] == (401,), route
        else:
            assert "node_zone_mismatch" not in declared


def test_payload_too_large_on_an_operation_without_body_is_the_a37_413() -> None:
    for route in (NodeRoute.CLIP_CONFIRMATION, NodeRoute.ZONE_CATALOG):
        assert "payload_too_large" not in _declared(route)
        assert status_of(route, NodeRejection(RejectionCode.PAYLOAD_TOO_LARGE)) == 413


def test_a_code_the_operation_does_not_admit_is_the_contract_transient() -> None:
    # zone_gate_not_approved solo en hallazgos y detecciones (BR-CTR-41).
    rendered = render(
        NodeRoute.HEARTBEAT, NodeRejection(RejectionCode.ZONE_GATE_NOT_APPROVED), VERSION
    )
    assert rendered.status == 503
    assert rendered.rejection is not None
    assert rendered.rejection.code is RejectionCode.TEMPORARILY_UNAVAILABLE


def test_rejected_newer_is_never_a_rejection_code() -> None:
    assert "rejected_newer" not in {code.value for code in RejectionCode}
    assert set(DEFAULT_ENUMERATIONS["rejection_code"]) == {code.value for code in RejectionCode}


def test_timestamp_out_of_window_is_permanent() -> None:
    body = body_for(NodeRejection(RejectionCode.TIMESTAMP_OUT_OF_WINDOW))
    assert body.retryable is False and body.retry_after_seconds is None
    assert status_of(NodeRoute.FINDING, NodeRejection(RejectionCode.TIMESTAMP_OUT_OF_WINDOW)) == 422


# --- Traducción de cada fallo --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NodeContextRejected(NodeContextReason.REVOKED), RejectionCode.NODE_REVOKED),
        (NodeContextRejected(NodeContextReason.NOT_ENROLLED), RejectionCode.NODE_NOT_ENROLLED),
        (NodeContextRejected(NodeContextReason.ZONE_MISMATCH), RejectionCode.NODE_ZONE_MISMATCH),
        (ContractValidationError("cameras[0].fps", "x"), RejectionCode.SCHEMA_INVALID),
        (
            ApiError(ApiErrorCode.RATE_LIMITED, retry_after_seconds=3_600),
            RejectionCode.RATE_LIMITED,
        ),
        (ApiError(ApiErrorCode.PAYLOAD_TOO_LARGE), RejectionCode.PAYLOAD_TOO_LARGE),
        (ApiError(ApiErrorCode.INVALID_REQUEST), RejectionCode.SCHEMA_INVALID),
        (ApiError(ApiErrorCode.INTERNAL_ERROR), RejectionCode.TEMPORARILY_UNAVAILABLE),
        (ApiError(ApiErrorCode.FORBIDDEN), RejectionCode.TEMPORARILY_UNAVAILABLE),
        (ApiError(ApiErrorCode.STORAGE_UNAVAILABLE), RejectionCode.STORAGE_UNAVAILABLE),
        (
            BulkheadSaturated(RouteClass.NODE, retry_after_seconds=5),
            RejectionCode.TEMPORARILY_UNAVAILABLE,
        ),
        (StorageUnavailable("put_object"), RejectionCode.STORAGE_UNAVAILABLE),
        (TemporarilyUnavailable(), RejectionCode.TEMPORARILY_UNAVAILABLE),
        (ExternalDependencyDown("correo"), RejectionCode.TEMPORARILY_UNAVAILABLE),
        (TimeoutError(), RejectionCode.TEMPORARILY_UNAVAILABLE),
        (
            CatalogRejected(CatalogDetailCode.ADMISSION_REJECTED),
            RejectionCode.TEMPORARILY_UNAVAILABLE,
        ),
        (
            RuntimeError("fleet.node_credential secreto-de-dominio"),
            RejectionCode.TEMPORARILY_UNAVAILABLE,
        ),
        (
            LedgerRejected(LedgerRejection.of(LedgerRejectionCode.IDEMPOTENCY_CONFLICT)),
            RejectionCode.IDEMPOTENCY_CONFLICT,
        ),
        (
            LedgerRejected(LedgerRejection.of(LedgerRejectionCode.EVIDENCE_MISSING)),
            RejectionCode.CLIP_MISSING,
        ),
        (
            LedgerRejected(LedgerRejection.of(LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT)),
            RejectionCode.TEMPORARILY_UNAVAILABLE,
        ),
        (
            LedgerRejected(LedgerRejection.of(LedgerRejectionCode.CONTEXT_ABSENT)),
            RejectionCode.TEMPORARILY_UNAVAILABLE,
        ),
    ],
)
def test_each_failure_translates_to_its_contract_code(
    error: BaseException, code: RejectionCode
) -> None:
    rejection = translate(error)
    assert rejection is not None and rejection.code is code
    body = body_for(rejection)
    assert body.retryable is (code in TRANSIENT_CODES)
    if body.retryable:
        assert body.retry_after_seconds is not None and 1 <= body.retry_after_seconds <= 60


@pytest.mark.parametrize(
    "error",
    [NotFound(), ApiError(ApiErrorCode.NOT_FOUND), HTTPException(404), HTTPException(405)],
)
def test_without_a_route_the_answer_is_404_without_body(error: BaseException) -> None:
    assert translate(error) is None
    rendered = render(None, error, VERSION)
    assert rendered.status == 404 and rendered.response.body == b""


# --- Metapropiedad ------------------------------------------------------------------------------

_MESSAGES = st.lists(st.sampled_from([*DOMAIN_WORDS, " ", ".", "_"]), max_size=6).map("".join)

FAILURES: st.SearchStrategy[BaseException] = st.one_of(
    st.sampled_from(list(RejectionCode)).map(NodeRejection),
    st.sampled_from(list(NodeContextReason)).map(NodeContextRejected),
    st.sampled_from(list(LedgerRejectionCode)).map(lambda c: LedgerRejected(LedgerRejection.of(c))),
    st.sampled_from([c for c in ApiErrorCode]).map(ApiError),
    st.builds(
        ContractValidationError,
        st.one_of(st.none(), st.sampled_from(["a.b", "x[0]", "ñ$"])),
        _MESSAGES,
    ),
    st.sampled_from([400, 401, 403, 404, 405, 413, 422, 500]).map(HTTPException),
    _MESSAGES.map(RuntimeError),
    _MESSAGES.map(KeyError),
    _MESSAGES.map(ValueError),
    st.integers(1, 3_600).map(lambda s: BulkheadSaturated(RouteClass.NODE, retry_after_seconds=s)),
    st.just(CatalogRejected(CatalogDetailCode.FAMILY_NOT_ADMITTED)),
    st.just(StorageUnavailable("head_object")),
    st.just(TemporarilyUnavailable()),
    st.just(NotFound()),
)


def _allowed(route: NodeRoute | None, code: str, status: int) -> bool:
    if route is None:
        return A37_STATUS[RejectionCode(code)] == status or (code, status) == (
            "schema_invalid",
            400,
        )
    declared = _declared(route)
    if code == "payload_too_large" and route.max_body_bytes == 0:
        return status == 413
    return status in declared.get(code, ())


def _clean(text: str, error: BaseException) -> None:
    for word in DOMAIN_WORDS:
        assert word not in text, word
    assert type(error).__name__ not in text
    if isinstance(error, RuntimeError | KeyError | ValueError | CatalogRejected):
        # El mensaje de una excepción ajena al contrato nunca llega al nodo.
        message = str(error).strip("'\"")
        if len(message) > 3:
            assert message not in text


@given(route=st.one_of(st.none(), st.sampled_from(list(NodeRoute))), error=FAILURES)
def test_any_failure_is_a_contract_rejection_never_an_api_error_nor_a_domain_name(
    route: NodeRoute | None, error: BaseException
) -> None:
    rendered = render(route, error, VERSION)
    text = bytes(rendered.response.body).decode()
    if rendered.status == 404:
        assert text == ""
        return
    body = parse_rejection_response(bytes(rendered.response.body))
    assert _allowed(route, body.code.value, rendered.status), (route, body.code, rendered.status)
    assert body.retryable is (body.code in TRANSIENT_CODES)
    if body.retryable:
        assert body.retry_after_seconds is not None and 1 <= body.retry_after_seconds <= 60
    else:
        assert body.retry_after_seconds is None
    assert body.message_es == MESSAGES[body.code] or body.code.value.startswith("contract_version")
    assert "correlation_id" not in text and "detail_code" not in text
    _clean(text, error)


@given(route=st.sampled_from([NodeRoute.ZONE_CATALOG, NodeRoute.CLIP_CONFIRMATION]), error=FAILURES)
def test_the_probe_route_answers_any_failure_of_its_operation_as_the_contract(
    route: NodeRoute, error: BaseException
) -> None:
    nodes = node_world()
    nodes.probe.failing = error
    path = route.path.replace("{zone_id}", str(zone_of(nodes.a))).replace(
        "{clip_id}", "0192f0c4-0000-7000-8000-0000000000c1"
    )
    with TestClient(nodes.app, raise_server_exceptions=False) as client:
        response = client.request(route.method, path, headers=nodes.headers(nodes.a))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-vigia-contract-version"] == VERSION
    assert not any(name.lower().startswith("access-control-") for name in response.headers)
    if response.status_code == 404:
        assert response.content == b""
        return
    body: Any = parse_rejection_response(response.content)
    assert _allowed(route, body.code.value, response.status_code)
    assert body.retryable is (body.code in TRANSIENT_CODES)
    _clean(response.text, error)
