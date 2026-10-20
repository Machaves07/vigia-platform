"""PR-NUC-38 (ampliada a cabeceras): cuerpos mutados y cabeceras de seguridad en toda respuesta.

- Para toda ruta con cuerpo o identificador del catálogo y toda mutación generada (campo extra,
  tipo cambiado, campo que falta, identificador malformado, JSON roto, tamaño excedido), la
  respuesta es ``invalid_request`` o ``payload_too_large`` con el mensaje genérico de su código y
  sin detalle interno (BR-NUC-92, 93).
- Límite de cuerpo (paso 4): 1 MB en toda ruta del catálogo, con o sin ``Content-Length``; la ruta
  que declara el suyo (``body_limit``) se rige por él.
- Cabeceras (paso 3, BR-NUC-95, NFR-NUC-18, pendientes nº 8 y 9): toda respuesta (éxito, error de
  la ruta, error de la cadena, ``429``, ``413``, ``500``, estáticos) lleva HSTS de un año con
  subdominios, ``nosniff``, ``DENY``, ``Referrer-Policy`` y la política de contenido de
  PAT-APP-SEG-01 con ``VIGIA_CSP_STORE_ORIGINS`` en ``media-src`` y ``connect-src``, sin
  ``unsafe-*`` y sin CORS.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.clock import SimulatedClock
from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.models.rejection_response import RejectionResponse

from tests.attachment_support import utc
from tests.factories import uuid7
from tests.middleware_support import (
    NODE_HEARTBEAT_PATH,
    NODE_READ_PATH,
    ORIGIN,
    SAME_ORIGIN,
    STORE_ORIGIN,
    Harness,
    UnlimitedRateLimiter,
    cookie_header,
)
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.monthly import audit as audit_module
from vigia_platform.accreditation.monthly import commitments as commitments_module
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.notifications.adapters.http.services import (
    NOTIFICATIONS_STATE_KEY,
    NotificationsHttp,
)
from vigia_platform.notifications.preferences import (
    NotificationPreference,
    NotificationPreferenceUpdate,
)
from vigia_platform.shared.api.app import (
    AppConfig,
    UnitRegistration,
    platform_permissions,
    platform_units,
)
from vigia_platform.shared.api.declarations import (
    NODE_PREFIX,
    DeclaredRoute,
    NodeRoute,
    iter_declared_routes,
    requires,
)
from vigia_platform.shared.api.errors import ApiErrorBody, ApiErrorCode
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.api.middleware import (
    DEFAULT_BODY_LIMIT_BYTES,
    content_security_policy,
    parse_store_origins,
)
from vigia_platform.shared.api.middleware.headers import store_origin
from vigia_platform.shared.context import Role

LABELS = PlatformLabels.load()
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "static"
EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    f"media-src 'self' {STORE_ORIGIN}; connect-src 'self' {STORE_ORIGIN}; "
    "frame-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'; require-trusted-types-for 'script'"
)
INTERNAL_MARKERS = ("Traceback", "pydantic", "ValidationError", "loc", "input", "File ", "line ")


# --- Catálogo de rutas con cuerpo ----------------------------------------------------------------

VALID_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/users"): {"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
    ("PATCH", "/users/{user_id}"): {"display_name": "Nombre sintético", "active": True},
    ("POST", "/auth/login"): {"email": "a@b.co", "password": "contraseña-sintética"},
    ("POST", "/loop/actions/{action_id}/attachments/upload-grant"): {
        "content_type": "image/png",
        "size_bytes": 10,
        "sha256": "a" * 64,
    },
    ("PUT", "/loop/plants/{plant_id}/configuration"): {
        "review_deadline_business_days": 7,
        "reason": "Ajuste acordado en el comité de seguridad",
    },
    ("POST", "/loop/findings/{finding_state_id}/classifications"): {
        "outcome": "confirmed",
        "reason_category": "real_coexistence",
        "reason_text": "Coexistencia observada entre la carretilla y el peatón en el pasillo",
    },
    ("POST", "/loop/findings/{finding_state_id}/actions"): {
        "action_type": "engineering",
        "action_description": "Instalar un resguardo fijo en el acceso a la zona",
        "assignee_user_id": str(uuid.uuid4()),
    },
    ("POST", "/loop/actions/{action_id}/evidence"): {
        "description": "Resguardo fijo instalado y anclado al piso en el acceso",
        "performed_at": "2026-10-01T10:00:00.000Z",
        "performed_by_role": "Técnico de mantenimiento",
    },
    # TASK-315: los campos opcionales que no cambian el resultado (referencias, categoría del paso,
    # ramas) quedan fuera, para que quitar o anular cualquiera de estos sea un rechazo.
    ("PUT", "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"): {
        "statement": "La carretilla cruzó el pasillo peatonal con la estación de carga energizada",
    },
    ("POST", "/loop/root-cause-analyses/{analysis_id}/signature"): {
        "professional_license": "LIC-SST-0001",
        "root_cause_category": "engineering",
        "root_cause_statement": "El trazado de la zona mezcla los flujos de carga y de paso",
        "conclusion": "Separar físicamente los flujos con un resguardo fijo en el acceso",
    },
    # ``professional_license`` es opcional a nivel de esquema (la licencia que falta es
    # ``signer_license_missing``, no ``invalid_request``): queda fuera, como ``verification_note``.
    ("POST", "/loop/findings/{finding_state_id}/closure"): {
        "closure_description": "Se verificó el resguardo fijo instalado en el acceso a la zona",
    },
    # ``reason_text`` es opcional a nivel de esquema (sin motivo es ``reopening_reason_required``,
    # no ``invalid_request``): queda fuera, como ``expected_outcome`` y ``deadline_days``.
    ("POST", "/loop/findings/{finding_state_id}/reopening"): {
        "action_type": "engineering",
        "action_description": "Ampliar el resguardo fijo para cubrir el acceso lateral",
        "assignee_user_id": str(uuid.uuid4()),
    },
    ("POST", "/accreditation/exports"): {
        "plant_id": str(uuid.uuid4()),
        "zone_ids": [str(uuid.uuid4())],
        "period": {"from": "2026-09-01T00:00:00.000Z", "to": "2026-10-01T00:00:00.000Z"},
        "package_form": "without_clips",
    },
    ("POST", "/loop/review-queue/{detection_id}/resolution"): {
        "outcome": "review_confirmed",
        "reason_category": "real_coexistence",
        "reason_text": "Coexistencia observada entre la carretilla y el peatón en el pasillo",
    },
    ("PUT", "/notifications/preferences"): {
        "email_enabled_normal": True,
        "email_enabled_digest": True,
        "weekly_digest_weekday": 1,
    },
    # TASK-347: ``comment`` y ``note`` son opcionales a nivel de esquema; quedan fuera para que
    # quitar o anular cualquiera de estos campos sea un rechazo.
    ("POST", "/accreditation/sample-items/{sample_item_id}/result"): {
        "outcome": "consistent_criterion",
    },
    ("POST", "/accreditation/monthly-packages/{package_id}/commitments"): {
        "commitments": [
            {
                "text": "Revisar la señalización del pasillo peatonal de la zona",
                "responsible_user_id": str(uuid.uuid4()),
                "due_date": "2026-12-01",
            }
        ]
    },
    ("PATCH", "/accreditation/commitments/{commitment_id}"): {"state": "done"},
    # TASK-350: ``removed``, ``declared_names_added`` y ``declared_names_removed`` son opcionales
    # y ``reason`` también a nivel de esquema (sin motivo es
    # ``vocabulary_change_reason_required``, no un cuerpo mal formado).
    ("POST", "/loop/vocabulary/changes"): {
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
}
"""Un cuerpo válido por ruta de personas con cuerpo del catálogo de la unidad de prueba. Las rutas
del contrato (``/api/nodes``) responden ``RejectionResponse``, no ``ApiError``: sus cuerpos los
prueban ``tests/properties/gob/test_pr_gob_02_prechecks_order.py`` y
``tests/unit/test_node_rejection_translation.py`` (TASK-206)."""


def _url(path: str, user_id: str | None = None) -> str:
    return (
        path.replace("{user_id}", user_id or str(uuid.uuid4()))
        .replace("{zone_id}", str(uuid.uuid4()))
        .replace("{action_id}", str(uuid.uuid4()))
        .replace("{plant_id}", str(uuid.uuid4()))
        .replace("{finding_state_id}", str(uuid.uuid4()))
        .replace("{analysis_id}", str(uuid.uuid4()))
        .replace("{detection_id}", str(uuid.uuid4()))
        .replace("{step_index}", "1")
        .replace("{sample_item_id}", str(uuid.uuid4()))
        .replace("{package_id}", str(uuid.uuid4()))
        .replace("{commitment_id}", str(uuid.uuid4()))
        # Ruta del contrato de VIG-152: el clip es un UUID v7 del nodo.
        .replace("{clip_id}", str(uuid7()))
    )


@pytest.fixture(scope="module")
def world() -> Iterator[tuple[Harness, Any, dict[str, str]]]:
    harness = Harness()
    cookie = harness.session("mutaciones")
    coordinator = harness.session("mutaciones-coordinacion", role=Role.COORDINATOR_SST)
    _ROLE_HEADERS.clear()
    _ROLE_HEADERS.update({**cookie_header(coordinator), **SAME_ORIGIN})
    # La tasa no es el asunto de este archivo (lo es de test_rate_limit.py).
    # El arnés pone en ``app.state`` los dobles de ``loop`` (``middleware_support.loop_state``): el
    # puerto de adjuntos real sobre memoria (sin acciones) y el doble de la configuración, para que
    # un cuerpo válido llegue a su servicio.
    app = harness.app(rate_limiter=UnlimitedRateLimiter())
    # La exportación (TASK-317) necesita la base: un cuerpo válido llega a un servicio que no
    # encuentra las zonas sintéticas (``not_found``), como el de adjuntos con la acción.
    # TASK-347: los dobles de la auditoría muestral y de los compromisos hacen las comprobaciones
    # del servicio que no miran la base y no encuentran ningún recurso sintético.
    setattr(
        app.state,
        ACCREDITATION_STATE_KEY,
        AccreditationHttp(
            exports=_NoZones(),  # type: ignore[arg-type]
            sample_audit=_NoSampleItems(),  # type: ignore[arg-type]
            commitments=_NoCommitments(),  # type: ignore[arg-type]
        ),
    )
    # TASK-348: ``PUT /notifications/preferences`` también necesita la base; el doble responde
    # con el cuerpo actualizado, como el resto de las rutas con cuerpo que sí tienen éxito (200).
    setattr(app.state, NOTIFICATIONS_STATE_KEY, NotificationsHttp(preferences=_FakePreferences()))  # type: ignore[arg-type]
    with TestClient(app, raise_server_exceptions=False) as client:
        yield harness, client, {**cookie_header(cookie), **SAME_ORIGIN}


class _NoZones:
    """``ExportRequests`` sin base: ninguna zona sintética existe (``ResourceNotFound``)."""

    clock = SimulatedClock(utc())

    async def request(self, context: Any, scope: Any) -> Any:
        raise ResourceNotFound()

    async def status(self, context: Any, export_id: Any) -> Any:
        raise ResourceNotFound()


class _NoSampleItems:
    """``SampleAuditService`` sin base (TASK-347): las comprobaciones del cuerpo con el mismo
    código del servicio; un cuerpo válido no encuentra ningún elemento sintético."""

    async def record_result(
        self,
        context: Any,
        sample_item_id: Any,
        *,
        outcome: object,
        comment: object = None,
        extra_fields: Any = None,
    ) -> Any:
        extras = extra_fields or {}
        if audit_module.modification_attempt(outcome, extras):
            raise audit_module.SampleAuditRejected(
                AccreditationDetailCode.SAMPLE_CANNOT_MODIFY_CLASSIFICATION
            )
        if extras:
            raise audit_module.SampleAuditRequestInvalid("campo desconocido")
        audit_module._outcome(outcome)
        audit_module._comment(comment)
        raise ResourceNotFound()


class _NoCommitments:
    """``CommitmentService`` sin base (TASK-347), como ``_NoSampleItems``."""

    async def record(self, context: Any, package_id: Any, drafts: Any) -> Any:
        for draft in drafts:
            commitments_module._text(draft.text)
            commitments_module._responsible(draft.responsible_user_id)
            commitments_module._due_date(draft.due_date)
        raise ResourceNotFound()

    async def update(
        self, context: Any, commitment_id: Any, *, state: object, note: object = None
    ) -> Any:
        commitments_module._note(note, commitments_module._state(state))
        raise ResourceNotFound()


class _FakePreferences:
    """``NotificationPreferences`` sin base: un cuerpo válido llega al servicio y vuelve (sin
    tocar ``FakeDatabase``, que no implementa ``transaction``, como ``_NoZones`` arriba)."""

    clock = SimulatedClock(utc())

    async def update_own(
        self, context: Any, update_values: NotificationPreferenceUpdate
    ) -> NotificationPreference:
        return NotificationPreference(
            user_id=context.actor.id,
            email_enabled_normal=update_values.email_enabled_normal,
            email_enabled_digest=update_values.email_enabled_digest,
            weekly_digest_weekday=update_values.weekly_digest_weekday,
            updated_at=self.clock.now(),
            email_suppressed_hard_bounce=False,
            email_suppressed_complaint=False,
            email_suppressed_at=None,
        )


_ROLE_HEADERS: dict[str, str] = {}
"""Sesión de coordinación SST: ``actions.manage``, ``findings.classify`` y ``export.create`` no
están en la columna del administrador, y sin la clave la ruta responde ``not_found`` antes de
validar el cuerpo (TASK-310, TASK-308, TASK-311 y TASK-317)."""
ROLE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/loop/actions/{action_id}/attachments/upload-grant"),
        ("POST", "/loop/findings/{finding_state_id}/classifications"),
        ("POST", "/loop/findings/{finding_state_id}/actions"),
        ("POST", "/loop/actions/{action_id}/evidence"),
        ("PUT", "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"),
        ("POST", "/loop/root-cause-analyses/{analysis_id}/signature"),
        ("POST", "/loop/findings/{finding_state_id}/closure"),
        ("POST", "/loop/findings/{finding_state_id}/reopening"),
        ("POST", "/accreditation/exports"),
        ("POST", "/loop/review-queue/{detection_id}/resolution"),
        # TASK-347: ``findings.classify`` y ``commitments.write`` (coordinación SST).
        ("POST", "/accreditation/sample-items/{sample_item_id}/result"),
        ("POST", "/accreditation/monthly-packages/{package_id}/commitments"),
        ("PATCH", "/accreditation/commitments/{commitment_id}"),
    }
)
ACCEPTED_STATUS: dict[tuple[str, str], int] = {
    # TASK-350: el doble acepta un cambio válido (la versión 2), como la ruta real (201).
    ("POST", "/loop/vocabulary/changes"): 201,
    ("POST", "/accreditation/sample-items/{sample_item_id}/result"): 404,
    ("POST", "/accreditation/monthly-packages/{package_id}/commitments"): 404,
    ("PATCH", "/accreditation/commitments/{commitment_id}"): 404,
    # Cuerpo válido: llega al servicio, que no encuentra la acción, el hallazgo (TASK-308,
    # ``ClassificationDouble``; TASK-311, ``ActionsDouble``), el análisis (TASK-315,
    # ``RootCauseDouble``), la detección (TASK-309, ``ReviewQueueDouble``) ni las zonas sintéticas.
    ("POST", "/loop/actions/{action_id}/attachments/upload-grant"): 404,
    ("POST", "/loop/findings/{finding_state_id}/classifications"): 404,
    ("POST", "/loop/findings/{finding_state_id}/actions"): 404,
    ("POST", "/loop/actions/{action_id}/evidence"): 404,
    ("PUT", "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"): 404,
    ("POST", "/loop/root-cause-analyses/{analysis_id}/signature"): 404,
    ("POST", "/loop/findings/{finding_state_id}/closure"): 404,
    ("POST", "/loop/findings/{finding_state_id}/reopening"): 404,
    ("POST", "/accreditation/exports"): 404,
    ("POST", "/loop/review-queue/{detection_id}/resolution"): 404,
}


def _headers_for(route: tuple[str, str], headers: dict[str, str]) -> dict[str, str]:
    return dict(_ROLE_HEADERS) if route in ROLE_ROUTES else headers


def _routes(client: Any) -> list[DeclaredRoute]:
    return list(iter_declared_routes(client.app.routes))


def _declared_detail_codes(client: Any, route: tuple[str, str]) -> frozenset[str]:
    """Los ``detail_code`` que declara la ruta ``(método, plantilla)``."""
    method, path = route
    return frozenset(
        code
        for declared in _routes(client)
        if declared.path == path and method in declared.methods
        for declaration in declared.declarations
        for code in declaration.detail_codes
    )


def test_the_catalog_of_body_routes_is_complete(world: tuple[Harness, Any, Any]) -> None:
    _, client, _ = world
    with_body = {
        (method, route.path)
        for route in _routes(client)
        if getattr(route.route, "body_field", None) is not None
        and not route.path.startswith(NODE_PREFIX + "/")
        for method in route.methods
    }
    assert with_body == set(VALID_BODIES)


def test_the_valid_bodies_are_accepted(world: tuple[Harness, Any, dict[str, str]]) -> None:
    _, client, headers = world
    for (method, path), body in VALID_BODIES.items():
        route_headers = _headers_for((method, path), headers)
        response = client.request(method, _url(path), headers=route_headers, json=body)
        expected = ACCEPTED_STATUS.get((method, path), 200)
        assert response.status_code == expected, (method, path, response.text)


MUTATIONS = st.sampled_from(
    ["extra", "type", "missing", "null", "array", "broken_json", "malformed_id", "oversized"]
)
ROUTES = st.sampled_from(sorted(VALID_BODIES))
WEIRD_VALUES = st.one_of(
    st.integers(), st.floats(allow_nan=False), st.booleans(), st.lists(st.integers(), max_size=3)
)


def _mutated(
    data: st.DataObject, mutation: str, route: tuple[str, str]
) -> tuple[str, bytes, dict[str, str]]:
    _, path = route
    body = dict(VALID_BODIES[route])
    url = _url(path)
    if mutation == "extra":
        name = data.draw(st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12))
        if name in body:
            name += "_extra"
        body[name] = data.draw(WEIRD_VALUES)
    elif mutation == "type":
        field = data.draw(st.sampled_from(sorted(body)))
        current = body[field]
        replacement = data.draw(
            WEIRD_VALUES.filter(lambda value: type(value) is not type(current))
            if not isinstance(current, str)
            else WEIRD_VALUES
        )
        body[field] = replacement
    elif mutation == "missing":
        del body[data.draw(st.sampled_from(sorted(body)))]
    elif mutation == "null":
        body[data.draw(st.sampled_from(sorted(body)))] = None
    elif mutation == "array":
        return url, json.dumps([body]).encode(), {"Content-Type": "application/json"}
    elif mutation == "broken_json":
        raw = json.dumps(body)
        cut = data.draw(st.integers(1, len(raw) - 1))
        return url, raw[:cut].encode(), {"Content-Type": "application/json"}
    elif mutation == "malformed_id":
        if "{user_id}" in path:
            bad = data.draw(st.sampled_from(["no-es-uuid", "1234", "zzzzzzzz-zzzz-zzzz-zzzz-zzzz"]))
            url = _url(path, bad)
        elif "{action_id}" in path:
            bad = data.draw(st.sampled_from(["no-es-uuid", "1234", "zzzzzzzz-zzzz-zzzz-zzzz-zzzz"]))
            url = path.replace("{action_id}", bad)
        elif "{plant_id}" in path:
            bad = data.draw(st.sampled_from(["no-es-uuid", "1234", "zzzzzzzz-zzzz-zzzz-zzzz-zzzz"]))
            url = path.replace("{plant_id}", bad)
        elif "{finding_state_id}" in path:
            bad = data.draw(st.sampled_from(["no-es-uuid", "1234", "zzzzzzzz-zzzz-zzzz-zzzz-zzzz"]))
            url = path.replace("{finding_state_id}", bad)
        elif "{analysis_id}" in path:
            bad = data.draw(st.sampled_from(["no-es-uuid", "1234", "zzzzzzzz-zzzz-zzzz-zzzz-zzzz"]))
            url = _url(path.replace("{analysis_id}", bad))
        elif "plant_id" in body:
            body["plant_id"] = data.draw(st.sampled_from(["no-es-uuid", "", "0" * 31]))
        else:
            # Sin identificador en la ruta: el campo de texto, vacío (fuera de su longitud mínima).
            body[next(iter(body))] = "" if isinstance(next(iter(body.values())), str) else "x"
    elif mutation == "oversized":
        body["relleno"] = "x" * (DEFAULT_BODY_LIMIT_BYTES + 1)
    return url, json.dumps(body).encode(), {"Content-Type": "application/json"}


def _generic_error(response: Any, detail_codes: frozenset[str] = frozenset()) -> ApiErrorBody:
    """El rechazo con el mensaje genérico de su código y sin detalle interno.

    ``detail_codes`` son los códigos de negocio que la ruta **declara** (``requires(...,
    detail_codes=...)``, A-68): uno de ellos puede acompañar al ``code`` (p. ej. un motivo que falta
    en ``PUT /loop/plants/{plant_id}/configuration``); cualquier otro ``detail_code``, no.
    """
    body = ApiErrorBody.model_validate_json(response.content)
    assert body.message_es == LABELS.label("api_error_code", body.code.value)
    allowed = {"code", "message_es", "correlation_id", "retry_after_seconds"}
    if body.detail_code is not None:
        assert body.detail_code in detail_codes, (body.detail_code, detail_codes)
        allowed.add("detail_code")
    assert set(response.json()) <= allowed
    for marker in INTERNAL_MARKERS:
        assert marker not in response.text
    return body


@given(data=st.data(), mutation=MUTATIONS, route=ROUTES)
def test_pr_nuc_38_every_mutated_body_is_a_generic_rejection(
    world: tuple[Harness, Any, dict[str, str]], data: st.DataObject, mutation: str, route: Any
) -> None:
    harness, client, headers = world
    handled = len(harness.observed.handled)
    url, content, extra = _mutated(data, mutation, route)
    route_headers = _headers_for(route, headers)
    response = client.request(route[0], url, headers={**route_headers, **extra}, content=content)
    body = _generic_error(response, _declared_detail_codes(client, route))
    expected = (
        {ApiErrorCode.PAYLOAD_TOO_LARGE}
        if mutation == "oversized"
        else {ApiErrorCode.INVALID_REQUEST}
    )
    assert body.code in expected, (mutation, route, response.text)
    assert len(harness.observed.handled) == handled, "el manejador no se ejecuta"
    _assert_security_headers(response)


# --- Límite de cuerpo en todo el catálogo --------------------------------------------------------


def _catalog(client: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for route in _routes(client):
        for method in sorted(route.methods):
            if method != "HEAD":
                path = route.path.replace("{screen_path:path}", "login").replace(
                    "{asset_path:path}", "x.js"
                )
                found.append((method, _url(path)))
    return found


def test_every_route_of_the_catalog_rejects_a_body_over_its_limit(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    _, client, headers = world
    oversized = b"x" * (DEFAULT_BODY_LIMIT_BYTES + 1)
    catalog = _catalog(client)
    assert len(catalog) >= 9
    for method, url in catalog:
        response = client.request(method, url, headers=headers, content=oversized)
        if url.startswith(NODE_PREFIX + "/"):
            # Ruta del contrato: el rechazo del contrato, nunca un ApiError (TASK-206).
            rejection = RejectionResponse.model_validate_json(response.content)
            assert rejection.code is RejectionCode.PAYLOAD_TOO_LARGE, (method, url)
        else:
            assert _generic_error(response).code is ApiErrorCode.PAYLOAD_TOO_LARGE, (method, url)
        assert response.status_code == 413
        _assert_security_headers(response)


def test_a_chunked_body_over_the_limit_is_payload_too_large(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    _, client, headers = world

    def chunks() -> Iterator[bytes]:
        for _ in range(17):
            yield b"x" * 65_536

    response = client.post("/users", headers=headers, content=chunks())
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert _generic_error(response).code is ApiErrorCode.PAYLOAD_TOO_LARGE


def test_a_body_exactly_at_the_limit_passes_the_chain(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    _, client, headers = world
    body = dict(VALID_BODIES[("POST", "/users")])
    raw = json.dumps(body).encode()
    padded = raw[:-1] + b" " * (DEFAULT_BODY_LIMIT_BYTES - len(raw)) + b"}"
    assert len(padded) == DEFAULT_BODY_LIMIT_BYTES
    response = client.post(
        "/users", headers={**headers, "Content-Type": "application/json"}, content=padded
    )
    assert response.status_code == 200


def test_a_route_with_its_own_limit_uses_it(world: tuple[Harness, Any, dict[str, str]]) -> None:
    _, client, _ = world
    raw = json.dumps({"sequence": 1}).encode()
    limit = NodeRoute.HEARTBEAT.max_body_bytes
    at_limit = raw + b" " * (limit - len(raw))
    over = at_limit + b" "
    json_type = {"Content-Type": "application/json"}
    assert client.post(NODE_HEARTBEAT_PATH, content=at_limit, headers=json_type).status_code == 200
    response = client.post(NODE_HEARTBEAT_PATH, content=over, headers=json_type)
    rejection = RejectionResponse.model_validate_json(response.content)
    assert rejection.code is RejectionCode.PAYLOAD_TOO_LARGE and response.status_code == 413
    # Un límite propio más pequeño no se aplica a las demás rutas.
    padded = json.dumps(VALID_BODIES[("POST", "/auth/login")]).encode() + b" " * 8000
    login = client.post("/auth/login", content=padded, headers={**json_type, **SAME_ORIGIN})
    assert login.status_code == 200


@pytest.mark.parametrize("value", ["abc", "-1", "1, 2", "1.5", " 12x", "99999999999999999999999"])
def test_a_malformed_content_length_is_invalid(
    world: tuple[Harness, Any, dict[str, str]], value: str
) -> None:
    _, client, _ = world
    scope_headers = [(b"content-length", value.encode()), (b"sec-fetch-site", b"same-origin")]
    response = _raw_request(client.app, "POST", "/auth/login", scope_headers, b"{}")
    assert response["status"] in (400, 413)
    _assert_raw_security_headers(response)


def _raw_request(
    app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]], body: bytes
) -> dict[str, Any]:
    """Una petición ASGI directa (para cabeceras que el cliente HTTP no deja mal formar)."""
    import asyncio

    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.9", 50000),
        "server": ("testserver", 443),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {"status": start["status"], "headers": start["headers"]}


def _raw_stream_status(
    app: Any, path: str, headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> int:
    """``GET`` ASGI directo con el cuerpo en varios mensajes (como uno ``chunked``)."""
    import asyncio

    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.10", 50000),
        "server": ("testserver", 443),
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    status: int = start["status"]
    return status


def test_r10_one_byte_over_the_limit_while_reading_the_body(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    # Seguimiento de VIG-78 (R10): sin Content-Length, el tope se mide al leer el cuerpo. La ruta
    # de salud responde 200 si el cuerpo pasa, así que 413 solo puede venir del límite.
    _, client, _ = world
    limit = DEFAULT_BODY_LIMIT_BYTES
    exact = [b"x" * (limit - 1), b"y"]
    over = [b"x" * (limit - 1), b"y", b"z"]
    assert _raw_stream_status(client.app, "/health/live", [], exact) == 200
    assert _raw_stream_status(client.app, "/health/live", [], over) == 413


@pytest.mark.parametrize("values", [("10", "11"), ("11", "10"), ("0", str(2**20 + 1))])
def test_r21_two_different_content_lengths_are_invalid(
    world: tuple[Harness, Any, dict[str, str]], values: tuple[str, str]
) -> None:
    # Seguimiento de VIG-78 (R21): dos Content-Length distintos no se resuelven tomando uno.
    _, client, _ = world
    headers = [(b"content-length", value.encode()) for value in values]
    assert _raw_stream_status(client.app, "/health/live", headers, [b"0123456789"]) == 400
    same = [(b"content-length", b"10"), (b"content-length", b"10")]
    assert _raw_stream_status(client.app, "/health/live", same, [b"0123456789"]) == 200


# --- Cabeceras de seguridad en toda respuesta ----------------------------------------------------


def _assert_security_headers(response: Any) -> None:
    headers = response.headers
    assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["content-security-policy"] == EXPECTED_CSP
    assert "unsafe-" not in headers["content-security-policy"]
    assert not [name for name in headers if name.lower().startswith("access-control-")]
    for name in ("content-security-policy", "x-frame-options", "strict-transport-security"):
        assert len(headers.get_list(name)) == 1


def _assert_raw_security_headers(response: dict[str, Any]) -> None:
    names = [bytes(name).lower() for name, _ in response["headers"]]
    for name in (b"strict-transport-security", b"x-content-type-options", b"x-frame-options",
                 b"referrer-policy", b"content-security-policy"):  # fmt: skip
        assert names.count(name) == 1


def _hostile_unit() -> UnitRegistration:
    router = APIRouter()

    @router.get("/hostil", dependencies=[requires("hierarchy.read")])
    async def hostile() -> JSONResponse:
        return JSONResponse(
            {"ok": True},
            headers={
                "Content-Security-Policy": "default-src * 'unsafe-inline' 'unsafe-eval'",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Credentials": "true",
                "X-Frame-Options": "ALLOWALL",
            },
        )

    @router.get("/rompe", dependencies=[requires("hierarchy.read")])
    async def broken() -> None:
        raise RuntimeError("SELECT * FROM identity.user_account")

    return UnitRegistration("hostil", routers=(router,))


def test_a_route_cannot_weaken_or_duplicate_the_security_headers() -> None:
    # Una ruta que intenta poner su propia política, CORS o marcos no rebaja nada.
    harness = Harness()
    app = harness.world.app(
        units=(*platform_units(), _hostile_unit()),
        permissions=platform_permissions(),
        csp_store_origins=(STORE_ORIGIN,),
        runtime={"authorizer": _AllowAll()},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/hostil")
    assert response.status_code == 200 and response.json() == {"ok": True}
    _assert_security_headers(response)


PATHS = st.sampled_from(
    ["/", "/me", "/users", "/health/live", "/health/ready", "/no-existe", "/assets/x.js",
     "/version.json", "/robots.txt", "/api/nodes/x", NODE_READ_PATH, NODE_HEARTBEAT_PATH,
     "/api/nodes/Findings", "/auth/login", "/docs", "/openapi.json"]
)  # fmt: skip
METHODS = st.sampled_from(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])


@given(path=PATHS, method=METHODS, navigate=st.booleans(), same_origin=st.booleans())
def test_pr_nuc_38_every_response_carries_the_security_headers(
    world: tuple[Harness, Any, dict[str, str]],
    path: str,
    method: str,
    navigate: bool,
    same_origin: bool,
) -> None:
    _, client, headers = world
    extra = dict(headers) if same_origin else {}
    if navigate:
        extra.update({"Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
    response = client.request(method, path, headers=extra)
    _assert_security_headers(response)


def test_static_files_and_screens_carry_the_security_headers() -> None:
    # Seguimiento de la revisión de VIG-71: index.html y assets/ con política y nosniff.
    harness = Harness()
    cookie = harness.session("estaticos")
    app = harness.world.app(
        units=(*platform_units(),),
        static_dir=FIXTURE,
        csp_store_origins=(STORE_ORIGIN,),
        public_origin=ORIGIN,
    )
    with TestClient(app) as client:
        index = client.get("/", headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
        asset = client.get("/assets/index-3f9a1c2b.js")
        version = client.get("/version.json")
        robots = client.get("/robots.txt")
        missing = client.get("/assets/no-existe.js", headers=cookie_header(cookie))
    assert index.status_code == asset.status_code == version.status_code == 200
    assert robots.status_code == 200 and missing.status_code == 404
    for response in (index, asset, version, robots, missing):
        _assert_security_headers(response)


def test_errors_of_the_chain_and_of_the_route_carry_the_security_headers() -> None:
    harness = Harness()
    cookie = harness.session("errores")
    app = harness.world.app(
        units=(*platform_units(), _hostile_unit()),
        permissions=platform_permissions(),
        csp_store_origins=(STORE_ORIGIN,),
        runtime={
            "sessions": harness.contexts,
            "privacy_notice_version": "v1-prueba",
            "authorizer": _AllowAll(),
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        broken = client.get("/rompe", headers=cookie_header(cookie))
        forbidden = client.post("/rompe")
    assert broken.status_code == 500 and broken.json()["code"] == "internal_error"
    assert "SELECT" not in broken.text
    assert forbidden.status_code == 403
    _assert_security_headers(broken)
    _assert_security_headers(forbidden)


class _AllowAll:
    async def authorize(self, request: Any, permission: str) -> None:
        return None


# --- Política de contenido y su variable ---------------------------------------------------------


def test_the_policy_composes_the_store_origins_without_unsafe_directives() -> None:
    other = "https://vigia-documents-000000000000-us-east-1.s3.us-east-1.amazonaws.com"
    policy = content_security_policy([STORE_ORIGIN, other])
    directives = dict(item.split(" ", 1) for item in policy.split("; "))
    assert directives["media-src"] == f"'self' {STORE_ORIGIN} {other}"
    assert directives["connect-src"] == f"'self' {STORE_ORIGIN} {other}"
    for name in ("default-src", "script-src", "style-src", "img-src", "base-uri", "form-action"):
        assert directives[name] == "'self'"
    for name in ("frame-src", "object-src", "frame-ancestors"):
        assert directives[name] == "'none'"
    assert directives["require-trusted-types-for"] == "'script'"
    assert "unsafe" not in policy and "*" not in policy and "data:" not in policy
    assert content_security_policy([]).count("media-src 'self';") == 1


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example 'unsafe-inline'",
        "https://a.example;script-src *",
        "https://*.amazonaws.com",
        "*",
        "'unsafe-eval'",
        "data:",
        "HTTPS://BUCKET.S3.AMAZONAWS.COM",
        "https://bucket.s3.amazonaws.com/",
        "https://bucket.s3.amazonaws.com/ruta",
        "http://bucket.s3.amazonaws.com",
        "https://bucket.s3.amazonaws.com:0",
        "https://bucket.s3.amazonaws.com,https://otro.example",
        "https://bu​cket.s3.amazonaws.com",
        "https://bucket.s3.amazonaws.com\t",
        "https://localhost",
        "javascript:alert(1)",
        "",
    ],
)
def test_a_hostile_store_origin_never_reaches_the_policy(value: str) -> None:
    with pytest.raises(ValueError):
        store_origin(value, allow_local=False)
    with pytest.raises(ValueError):
        AppConfig(environment="pilot", data_key_id="alias/x", csp_store_origins=(value,))


def test_the_variable_is_a_space_separated_list_read_from_the_environment() -> None:
    other = "https://otro-bucket.s3.us-east-1.amazonaws.com"
    config = AppConfig.from_environ(
        {
            "VIGIA_ENVIRONMENT": "pilot",
            "VIGIA_SECRETS_KEY_ARN": "alias/x",
            "VIGIA_CSP_STORE_ORIGINS": f"  {STORE_ORIGIN}   {other} ",
            "VIGIA_PUBLIC_ORIGIN": "https://app.vigia.example",
        }
    )
    assert config.csp_store_origins == (STORE_ORIGIN, other)
    assert config.public_origin == "https://app.vigia.example"
    assert parse_store_origins(f"{STORE_ORIGIN} {STORE_ORIGIN}", allow_local=False) == (
        STORE_ORIGIN,
    )
    with pytest.raises(ValueError):
        AppConfig(
            environment="pilot",
            data_key_id="alias/x",
            csp_store_origins=(STORE_ORIGIN, STORE_ORIGIN),
        )
    with pytest.raises(ValueError):
        AppConfig(
            environment="pilot",
            data_key_id="alias/x",
            csp_store_origins=tuple(f"https://b{i}.s3.amazonaws.com" for i in range(9)),
        )


def test_local_store_origins_are_only_admitted_in_local_and_test() -> None:
    local = "http://localhost:4566"
    assert AppConfig(environment="local", data_key_id="a", csp_store_origins=(local,))
    assert AppConfig(environment="test", data_key_id="a", public_origin="http://127.0.0.1:8000")
    for environment in ("pilot", "staging-1"):
        with pytest.raises(ValueError):
            AppConfig(environment=environment, data_key_id="a", csp_store_origins=(local,))
        with pytest.raises(ValueError):
            AppConfig(environment=environment, data_key_id="a", public_origin=local)
