"""PR-NUC-53: barrera anti-falsificación por metadatos de la petición (PAT-NUC-SEG-02; paso 8).

- Para toda ruta del catálogo que cambia estado (métodos distintos de ``GET``, ``HEAD`` y
  ``OPTIONS``), una petición con sesión válida sin ``Sec-Fetch-Site: same-origin`` o con un
  ``Origin`` distinto del configurado responde ``forbidden`` (``code = forbidden``) **sin efecto**
  (el manejador no se ejecuta) y deja ``csrf_rejected`` en la auditoría; con ambas correctas, se
  procesa.
- ``csrf_rejected`` es solo una operación de auditoría, nunca un código de error (nota del
  2026-09-23 de PAT-NUC-SEG-02). Sin sesión, va a la cadena de la organización proveedora.
- Las rutas de nodos quedan fuera por construcción; los métodos seguros no pasan la barrera; una
  auditoría caída no cambia la respuesta.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from tests.middleware_support import (
    CLIENT_ORG,
    NODE_HEARTBEAT_PATH,
    ORIGIN,
    PROVIDER_ORG,
    SAME_ORIGIN,
    Harness,
    UnlimitedRateLimiter,
    cookie_header,
)
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditOutcome
from vigia_platform.shared.api.declarations import iter_declared_routes
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.api.middleware import (
    AuditCsrfRejections,
    CsrfReason,
    CsrfRejection,
)
from vigia_platform.shared.api.middleware.steps import SAFE_METHODS
from vigia_platform.shared.context import Role
from vigia_platform.shared.db import RouteClass

LABELS = PlatformLabels.load()

BODIES: dict[str, dict[str, Any]] = {
    "/users": {"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
    "/users/{user_id}": {"display_name": "Nombre sintético", "active": True},
    "/auth/login": {"email": "a@b.co", "password": "contraseña-sintética"},
    "/privacy-notice/accept": {},
    "/notifications/{notification_id}/read": {},
    "/notifications/preferences": {
        "email_enabled_normal": True,
        "email_enabled_digest": True,
        "weekly_digest_weekday": 1,
    },
    "/platform/dead-letter/{event_id}/{consumer}/replay": {},
    "/platform/keys/{purpose}/rotate": {},
    "/loop/actions/{action_id}/attachments/upload-grant": {
        "content_type": "image/png",
        "size_bytes": 10,
        "sha256": "a" * 64,
    },
    "/loop/attachments/{attachment_id}/read-url": {},
    "/loop/plants/{plant_id}/configuration": {
        "review_deadline_business_days": 7,
        "reason": "Ajuste acordado en el comité de seguridad",
    },
    "/loop/findings/{finding_state_id}/classifications": {
        "outcome": "confirmed",
        "reason_category": "real_coexistence",
        "reason_text": "Coexistencia observada entre la carretilla y el peatón en el pasillo",
    },
    "/loop/review-queue/{detection_id}/resolution": {
        "outcome": "review_confirmed",
        "reason_category": "real_coexistence",
        "reason_text": "Coexistencia observada entre la carretilla y el peatón en el pasillo",
    },
    "/loop/findings/{finding_state_id}/actions": {
        "action_type": "engineering",
        "action_description": "Instalar un resguardo fijo en el acceso a la zona",
        "assignee_user_id": str(uuid.uuid4()),
    },
    "/loop/actions/{action_id}/evidence": {
        "description": "Resguardo fijo instalado y anclado al piso en el acceso",
        "performed_at": "2026-10-01T10:00:00.000Z",
        "performed_by_role": "Técnico de mantenimiento",
    },
    "/loop/findings/{finding_state_id}/closure": {
        "closure_description": "Resguardo fijo verificado y anclado en el acceso de la zona",
        "professional_license": "LIC-SST-0001",
    },
    "/loop/findings/{finding_state_id}/reopening": {
        "reason_text": "El resguardo se retiró durante el mantenimiento mayor",
        "action_type": "engineering",
        "action_description": "Reinstalar el resguardo fijo en el acceso de la zona",
        "assignee_user_id": str(uuid.uuid4()),
    },
    "/loop/findings/{finding_state_id}/root-cause-analyses": {},
    "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}": {
        "statement": "La carretilla cruzó el pasillo peatonal con la estación de carga energizada",
    },
    "/loop/root-cause-analyses/{analysis_id}/signature": {
        "professional_license": "LIC-SST-0001",
        "root_cause_category": "engineering",
        "root_cause_statement": "El trazado de la zona mezcla los flujos de carga y de paso",
        "conclusion": "Separar físicamente los flujos con un resguardo fijo en el acceso",
    },
    "/loop/root-cause-analyses/{analysis_id}/assistant-requests": {},
    "/loop/vocabulary/changes": {
        "added": [
            {
                "category": "absence_claim",
                "form": "lemma",
                "value": "pasillo",
                "suggestion_es": "Describa lo observado en la zona",
            }
        ],
        "reason": "Ajuste acordado en el comité de seguridad",
    },
    "/loop/findings/{finding_state_id}/unclosable": {
        "evaluated_alternative": "Reubicar la estación de carga al costado norte de la zona",
        "rejection_reason": "La reubicación deja el pasillo peatonal sin salida de emergencia",
        "responsible_user_id": str(uuid.uuid4()),
        "decision_date": "2026-10-01",
        "compensatory_measure": "Vigilancia peatonal permanente durante el turno de carga",
        "professional_license": "LIC-SST-0001",
    },
    "/loop/unclosable/{declaration_id}/reviews": {
        "outcome": "renewed",
        "review_note": "La alternativa sigue descartada por el trazado de la zona",
        "professional_license": "LIC-SST-0001",
    },
    "/accreditation/exports": {
        "plant_id": str(uuid.uuid4()),
        "zone_ids": [str(uuid.uuid4())],
        "period": {"from": "2026-09-01T00:00:00.000Z", "to": "2026-10-01T00:00:00.000Z"},
        "package_form": "without_clips",
    },
    "/accreditation/exports/{export_id}/download-url": {},
    "/accreditation/sample-items/{sample_item_id}/result": {"outcome": "consistent_criterion"},
    "/accreditation/monthly-packages/{package_id}/commitments": {
        "commitments": [
            {
                "text": "Revisar la señalización del pasillo peatonal de la zona",
                "responsible_user_id": str(uuid.uuid4()),
                "due_date": "2026-12-01",
            }
        ]
    },
    "/accreditation/commitments/{commitment_id}": {"state": "done"},
}
OPERATION_PREFIX = "/platform/"
"""Rutas reales de operación (TASK-137): sin servicios en el arnés no tienen efecto observable, así
que entran en las falsificadas (``forbidden`` sin efecto) y no en las que se procesan."""
REAL_SERVICE_PREFIXES = (OPERATION_PREFIX, "/loop/", "/accreditation/", "/notifications/")
"""Rutas con servicios reales o dobles (operación, adjuntos de cierre de U-04, TASK-310,
configuración del lazo, TASK-304, exportación, TASK-317, y marcar leída/preferencias, TASK-348):
se prueban entre las falsificadas; su procesamiento, en ``tests/properties/laz``,
``tests/integration/test_loop_configuration_routes.py``,
``tests/integration/test_export_request_routes.py`` y
``tests/isolation/test_route_isolation.py``/``tests/integration/test_notification_routes.py``."""


def _state_changing_routes(app: Any) -> list[tuple[str, str]]:
    """Las rutas de personas del catálogo que cambian estado (metapropiedad sobre el catálogo)."""
    found: list[tuple[str, str]] = []
    for route in iter_declared_routes(app.routes):
        if route.path.startswith("/api/nodes"):
            continue
        for method in sorted(route.methods - SAFE_METHODS):
            found.append((method, route.path))
    return found


def _url(path: str) -> str:
    return (
        path.replace("{user_id}", str(uuid.uuid4()))
        .replace("{event_id}", str(uuid.uuid4()))
        .replace("{consumer}", "alert_metrics")
        .replace("{purpose}", "checkpoint")
        .replace("{action_id}", str(uuid.uuid4()))
        .replace("{attachment_id}", str(uuid.uuid4()))
        .replace("{plant_id}", str(uuid.uuid4()))
        .replace("{export_id}", str(uuid.uuid4()))
        .replace("{finding_state_id}", str(uuid.uuid4()))
        .replace("{detection_id}", str(uuid.uuid4()))
        .replace("{analysis_id}", str(uuid.uuid4()))
        .replace("{step_index}", "1")
        .replace("{notification_id}", str(uuid.uuid4()))
        .replace("{sample_item_id}", str(uuid.uuid4()))
        .replace("{package_id}", str(uuid.uuid4()))
        .replace("{commitment_id}", str(uuid.uuid4()))
        .replace("{declaration_id}", str(uuid.uuid4()))
    )


def _forbidden(response: Any) -> None:
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == ApiErrorCode.FORBIDDEN == "forbidden"
    assert body["message_es"] == LABELS.label("api_error_code", "forbidden")
    assert "csrf" not in response.text.lower()


@pytest.fixture
def harness() -> Harness:
    return Harness()


def test_the_catalog_has_state_changing_routes(harness: Harness) -> None:
    routes = _state_changing_routes(harness.app())
    # Las de operación de la plataforma (TASK-137) son de la unidad ``shared``.
    assert routes == [
        ("POST", "/platform/dead-letter/{event_id}/{consumer}/replay"),
        ("POST", "/platform/keys/{purpose}/rotate"),
        # Adjuntos de cierre de U-04 (TASK-310).
        ("POST", "/loop/actions/{action_id}/attachments/upload-grant"),
        ("POST", "/loop/attachments/{attachment_id}/read-url"),
        # Configuración del lazo de U-04 (TASK-304).
        ("PUT", "/loop/plants/{plant_id}/configuration"),
        # Clasificación de un hallazgo de U-04 (TASK-308).
        ("POST", "/loop/findings/{finding_state_id}/classifications"),
        # Resolución de la cola de revisión de U-04 (TASK-309).
        ("POST", "/loop/review-queue/{detection_id}/resolution"),
        # Acción correctiva y su evidencia de U-04 (TASK-311).
        ("POST", "/loop/findings/{finding_state_id}/actions"),
        ("POST", "/loop/actions/{action_id}/evidence"),
        # Cierre firmado y reapertura de un hallazgo de U-04 (TASK-343, VIG-233).
        ("POST", "/loop/findings/{finding_state_id}/closure"),
        ("POST", "/loop/findings/{finding_state_id}/reopening"),
        # Análisis de causa raíz guiado de U-04 (TASK-315).
        ("POST", "/loop/findings/{finding_state_id}/root-cause-analyses"),
        ("PUT", "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"),
        ("POST", "/loop/root-cause-analyses/{analysis_id}/signature"),
        # Solicitud al asistente de causa raíz de U-04 (TASK-316).
        ("POST", "/loop/root-cause-analyses/{analysis_id}/assistant-requests"),
        # Cambio de la lista de vocabulario de U-04 (TASK-350).
        ("POST", "/loop/vocabulary/changes"),
        # Declaración del incerrable y su revisión de U-04 (TASK-312).
        ("POST", "/loop/findings/{finding_state_id}/unclosable"),
        ("POST", "/loop/unclosable/{declaration_id}/reviews"),
        # Solicitud de exportación de U-04 (TASK-317).
        ("POST", "/accreditation/exports"),
        # URL de descarga del documento de una exportación (TASK-345).
        ("POST", "/accreditation/exports/{export_id}/download-url"),
        # Resultado de la auditoría muestral y compromisos del mando de línea (TASK-347).
        ("POST", "/accreditation/sample-items/{sample_item_id}/result"),
        ("POST", "/accreditation/monthly-packages/{package_id}/commitments"),
        ("PATCH", "/accreditation/commitments/{commitment_id}"),
        # Marcar leída y preferencias de notificaciones de U-04 (TASK-348).
        ("POST", "/notifications/{notification_id}/read"),
        ("PUT", "/notifications/preferences"),
        ("POST", "/users"),
        ("PATCH", "/users/{user_id}"),
        ("POST", "/privacy-notice/accept"),
        ("POST", "/auth/login"),
    ]


BAD_FETCH_SITE = st.one_of(
    st.none(),
    st.sampled_from(
        ["cross-site", "same-site", "none", "Same-Origin", "same-origin ", " same-origin", ""]
    ),
    st.text(min_size=1, max_size=20).filter(lambda value: value.strip() != "same-origin"),
)
BAD_ORIGIN = st.one_of(
    st.sampled_from(
        [
            "null",
            "https://evil.example",
            ORIGIN + "/",
            ORIGIN.upper(),
            ORIGIN.replace("https", "http"),
            ORIGIN + ":443",
            "https://app.vigia.test.evil.example",
            "",
        ]
    ),
    st.text(min_size=1, max_size=40).filter(lambda value: value != ORIGIN),
)


def _headers(fetch_site: str | None, origin: str | None) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    if fetch_site is not None:
        headers.append(("Sec-Fetch-Site", fetch_site))
    if origin is not None:
        headers.append(("Origin", origin))
    return headers


def _ascii(values: list[tuple[str, str]]) -> bool:
    return all(value.isascii() and value.isprintable() for _, value in values)


@given(
    data=st.data(),
    fetch_site=BAD_FETCH_SITE,
    origin=st.one_of(st.none(), st.just(ORIGIN), BAD_ORIGIN),
    good_fetch_bad_origin=st.booleans(),
)
def test_pr_nuc_53_forged_requests_are_forbidden_without_effect(
    data: st.DataObject, fetch_site: str | None, origin: str | None, good_fetch_bad_origin: bool
) -> None:
    harness = Harness()
    cookie = harness.session("pr-nuc-53")
    app = harness.app(rate_limiter=UnlimitedRateLimiter())
    method, path = data.draw(st.sampled_from(_state_changing_routes(app)))
    if good_fetch_bad_origin:
        fetch_site, origin = "same-origin", data.draw(BAD_ORIGIN)
    headers = _headers(fetch_site, origin)
    if not _ascii(headers):
        return  # una cabecera HTTP no puede llevar esos caracteres
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(
            method,
            _url(path),
            headers=[*cookie_header(cookie).items(), *headers],
            json=BODIES[path],
        )
    _forbidden(response)
    assert harness.observed.handled == [], "una petición falsificada no tiene efecto"
    if path == "/auth/login":
        # Ruta pública: la cadena no valida la sesión, así que va a la cadena de la proveedora.
        (rejection,) = harness.csrf_audit.without_session
        assert harness.csrf_audit.with_session == []
    else:
        ((context, rejection),) = harness.csrf_audit.with_session
        assert context.organization_id == CLIENT_ORG
        assert str(context.correlation_id) == response.json()["correlation_id"]
    assert rejection.route == path and rejection.method == method
    assert rejection.origin_hash is not None and len(rejection.origin_hash) == 64


@given(data=st.data(), with_origin=st.booleans())
def test_pr_nuc_53_with_both_headers_right_the_request_is_processed(
    data: st.DataObject, with_origin: bool
) -> None:
    harness = Harness()
    cookie = harness.session("pr-nuc-53-ok")
    app = harness.app(rate_limiter=UnlimitedRateLimiter())
    processed = [
        r for r in _state_changing_routes(app) if not r[1].startswith(REAL_SERVICE_PREFIXES)
    ]
    method, path = data.draw(st.sampled_from(processed))
    headers = {**cookie_header(cookie), **SAME_ORIGIN}
    if with_origin:
        headers["Origin"] = ORIGIN
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(method, _url(path), headers=headers, json=BODIES[path])
    assert response.status_code == 200, response.text
    assert len(harness.observed.handled) == 1
    assert harness.csrf_audit.total == 0


def test_a_post_without_sec_fetch_site_is_forbidden_and_audited(harness: Harness) -> None:
    cookie = harness.session("sin-cabecera")
    with TestClient(harness.app()) as client:
        response = client.post("/users", headers=cookie_header(cookie), json=BODIES["/users"])
    _forbidden(response)
    ((_, rejection),) = harness.csrf_audit.with_session
    assert rejection.reason is CsrfReason.FETCH_SITE_MISSING
    assert harness.observed.handled == []


def test_a_forged_request_without_session_is_audited_in_the_provider_chain(
    harness: Harness,
) -> None:
    with TestClient(harness.app()) as client:
        login = client.post(
            "/auth/login", headers={"Sec-Fetch-Site": "cross-site"}, json=BODIES["/auth/login"]
        )
        # Sin cookie en una ruta con permiso: también se rechaza antes de autorizar.
        users = client.post("/users", json=BODIES["/users"])
    _forbidden(login)
    _forbidden(users)
    assert [r.reason for r in harness.csrf_audit.without_session] == [
        CsrfReason.FETCH_SITE_NOT_SAME_ORIGIN,
        CsrfReason.FETCH_SITE_MISSING,
    ]
    assert [r.route for r in harness.csrf_audit.without_session] == ["/auth/login", "/users"]
    assert harness.csrf_audit.with_session == []


def test_unmatched_state_changing_requests_are_also_stopped(harness: Harness) -> None:
    with TestClient(harness.app()) as client:
        response = client.delete("/no-existe")
    _forbidden(response)
    (rejection,) = harness.csrf_audit.without_session
    assert rejection.route == "unmatched" and rejection.method == "DELETE"


def test_a_repeated_sec_fetch_site_or_origin_is_ambiguous_and_forbidden(harness: Harness) -> None:
    cookie = harness.session("repetidas")
    with TestClient(harness.app()) as client:
        twice = client.post(
            "/privacy-notice/accept",
            headers=[
                *cookie_header(cookie).items(),
                ("Sec-Fetch-Site", "same-origin"),
                ("Sec-Fetch-Site", "same-origin"),
            ],
        )
        origins = client.post(
            "/privacy-notice/accept",
            headers=[
                *cookie_header(cookie).items(),
                ("Sec-Fetch-Site", "same-origin"),
                ("Origin", ORIGIN),
                ("Origin", ORIGIN),
            ],
        )
    _forbidden(twice)
    _forbidden(origins)
    assert harness.observed.handled == []


def test_without_a_configured_origin_any_origin_header_is_forbidden() -> None:
    harness = Harness(public_origin=None)
    cookie = harness.session("sin-origen")
    with TestClient(harness.app()) as client:
        with_origin = client.post(
            "/privacy-notice/accept",
            headers={**cookie_header(cookie), **SAME_ORIGIN, "Origin": ORIGIN},
        )
        without_origin = client.post(
            "/privacy-notice/accept", headers={**cookie_header(cookie), **SAME_ORIGIN}
        )
    _forbidden(with_origin)
    assert without_origin.status_code == 200
    ((_, rejection),) = harness.csrf_audit.with_session
    assert rejection.reason is CsrfReason.ORIGIN_MISMATCH


@pytest.mark.parametrize("method", sorted(SAFE_METHODS))
def test_safe_methods_do_not_pass_the_barrier(harness: Harness, method: str) -> None:
    cookie = harness.session("seguros")
    with TestClient(harness.app()) as client:
        response = client.request(
            method, "/me", headers={**cookie_header(cookie), "Sec-Fetch-Site": "cross-site"}
        )
    assert response.status_code != 403
    assert harness.csrf_audit.total == 0


def test_node_routes_are_outside_the_barrier_by_construction(harness: Harness) -> None:
    with TestClient(harness.app()) as client:
        response = client.post(
            NODE_HEARTBEAT_PATH,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
            json={"sequence": 1},
        )
    assert response.status_code == 200
    assert harness.observed.route_classes == [RouteClass.NODE]
    assert harness.csrf_audit.total == 0


def test_a_failing_audit_does_not_change_the_answer(harness: Harness) -> None:
    harness.csrf_audit.fail = True
    cookie = harness.session("auditoria-caida")
    with TestClient(harness.app(), raise_server_exceptions=False) as client:
        response = client.post("/users", headers=cookie_header(cookie), json=BODIES["/users"])
        anonymous = client.post("/auth/login", json=BODIES["/auth/login"])
    _forbidden(response)
    _forbidden(anonymous)
    assert harness.observed.handled == []


def test_the_barrier_runs_after_the_session_and_before_the_privacy_notice() -> None:
    # Sesión sin aviso vigente y petición falsificada: responde la barrera (paso 8), no el aviso
    # (paso 9); con una sesión inválida responde la sesión (paso 6).
    harness = Harness()
    cookie = harness.session("orden", accepted=None)
    with TestClient(harness.app()) as client:
        forged = client.post("/users", headers=cookie_header(cookie), json=BODIES["/users"])
        broken = client.post(
            "/users", headers={"Cookie": "__Host-vigia_session=x"}, json=BODIES["/users"]
        )
    _forbidden(forged)
    assert broken.status_code == 401


# --- Adaptador sobre AuditWriter ----------------------------------------------------------------


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, AuditOperation, dict[str, Any]]] = []

    async def append(self, context: Any, operation: AuditOperation, **fields: Any) -> None:
        self.calls.append(("append", context, operation, fields))

    async def append_without_organization(
        self, context: Any, operation: AuditOperation, **fields: Any
    ) -> None:
        self.calls.append(("without_organization", context, operation, fields))


def test_the_adapter_writes_csrf_rejected_as_a_denied_audit_operation() -> None:
    import asyncio

    from tests.factories import make_context

    writer = _RecordingWriter()
    provider = make_context(organization_id=PROVIDER_ORG)
    adapter = AuditCsrfRejections(audit=writer, provider_context=lambda: provider)  # type: ignore[arg-type]
    session = make_context(organization_id=CLIENT_ORG)
    rejection = CsrfRejection(CsrfReason.ORIGIN_MISMATCH, "POST", "/users", "f" * 64)
    anonymous = CsrfRejection(CsrfReason.FETCH_SITE_MISSING, "POST", "/auth/login", None)
    asyncio.run(adapter.csrf_rejected(session, rejection))
    asyncio.run(adapter.csrf_rejected_without_session(anonymous))
    (with_session, without_session) = writer.calls
    assert with_session[:3] == ("append", session, AuditOperation.CSRF_REJECTED)
    assert with_session[3] == {
        "outcome": AuditOutcome.DENIED,
        "filters": {
            "reason": "origin_mismatch",
            "method": "POST",
            "route": "/users",
            "origin_hash": "f" * 64,
        },
    }
    assert without_session[:3] == ("without_organization", provider, AuditOperation.CSRF_REJECTED)
    assert without_session[3]["filters"] == {
        "reason": "fetch_site_missing",
        "method": "POST",
        "route": "/auth/login",
    }
    assert AuditOperation.CSRF_REJECTED.value == "csrf_rejected"
    assert "csrf_rejected" not in {code.value for code in ApiErrorCode}


def test_the_role_does_not_matter_to_the_barrier() -> None:
    harness = Harness()
    cookie = harness.session("copasst", role=Role.COPASST)
    with TestClient(harness.app()) as client:
        response = client.post("/privacy-notice/accept", headers=cookie_header(cookie))
    _forbidden(response)
