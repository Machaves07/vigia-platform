"""N-4 · Acceso permanente del proveedor (T4; business-rules §14).

**Qué intenta**: una persona del proveedor mantiene acceso a los datos de un cliente sin
vencimiento, o sin que el cliente lo vea.

**Qué lo detiene** (BR-NUC-35 a BR-NUC-42):

- BR-NUC-35: la concesión es sobre una organización cliente activa, con motivo y duración acotada
  por el tope del cliente; sin duración, la de omisión; no se prorroga;
- BR-NUC-36 y BR-NUC-41: entra en vigor dejando ``provider_concession_granted`` en la cadena del
  cliente, que ve todas sus concesiones y cada ``provider_query``;
- BR-NUC-37 y BR-NUC-38: bajo concesión solo la columna ``provider_installer``; cada petición
  autorizada deja su ``provider_query`` en la cadena del cliente;
- BR-NUC-39 y BR-NUC-40: revocada o vencida, la petición siguiente ya no construye contexto, sin
  esperar a la tarea periódica; las sesiones del proveedor siguen vivas para su organización;
- BR-NUC-42: no hay concesión sobre la proveedora, ni permanente, ni sin persona identificada.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.platform_support import T0, Platform, code_of, stamp
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

REASON = "Revisión sintética del nodo de la línea 3"


def _grant(platform: Platform, cookie: Any, organization_id: uuid.UUID, **changes: Any) -> Any:
    body: dict[str, Any] = {
        "client_organization_id": str(organization_id),
        "scope_level": "organization",
        "scope_id": str(organization_id),
        "reason": REASON,
    }
    body.update(changes)
    return platform.call("POST", "/provider/concessions", cookie=cookie, json_body=body)


def _coverage(
    platform: Platform, cookie: Any, zone_id: uuid.UUID, concession: uuid.UUID
) -> httpx.Response:
    return platform.call(
        "GET",
        f"/zones/{zone_id}/coverage",
        cookie=cookie,
        concession=concession,
        params={"from": stamp(T0), "to": stamp(T0 + timedelta(hours=1))},
    )


def test_n04_no_concession_is_permanent_or_beyond_the_clients_limit(platform: Platform) -> None:
    site = platform.site()
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    limits = platform.call(
        "PATCH",
        "/organization/settings",
        cookie=admin,
        json_body={"concession_max_days": 3, "concession_default_days": 2},
    )
    assert limits.status_code == 200, limits.text
    _, installer = platform.installer()
    for duration in (3 * 24 + 1, 0, -1, 10**9, 2**63, "7d", 1.5, True):
        response = _grant(platform, installer, site.organization_id, duration_hours=duration)
        assert response.status_code == 400, (duration, response.text)
    # Sobre la propia proveedora, o sin organización cliente real: como inexistente.
    for target in (platform.provider, uuid.uuid4()):
        assert _grant(platform, installer, target).status_code == 404
    # Sin duración: la de omisión del cliente, nunca indefinida.
    granted = _grant(platform, installer, site.organization_id)
    assert granted.status_code == 201, granted.text
    body = granted.json()
    lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(
        body["granted_at"]
    )
    assert lifetime == timedelta(days=2)
    # El registro en el expediente del cliente es la constancia (BR-NUC-36).
    (record,) = [
        r
        for r in platform.records(site.organization_id, "provider_concession_granted")
        if json.loads(r["content_json"])["concession_id"] == body["concession_id"]
    ]
    assert record["actor_kind"] == "provider_user"


def test_n04_an_expired_concession_stops_before_the_periodic_task_runs(platform: Platform) -> None:
    site = platform.site()
    ((_, zone_id),) = site.zones()
    installer_id, installer = platform.installer()
    authz = platform.authz
    # Venció hace un minuto: la tarea ``expire_concessions`` aún no la ha marcado.
    expired = authz.add_concession(
        site.organization_id,
        installer_id,
        granted_at=authz.now() - timedelta(days=7, minutes=1),
        duration=timedelta(days=7),
    )
    (row,) = platform.fetch(
        "SELECT status FROM identity.provider_concession WHERE concession_id = $1", expired
    )
    assert row["status"] == "active"
    response = _coverage(platform, installer, zone_id, expired)
    assert code_of(response) in ("not_found", "unauthenticated"), response.text
    assert str(zone_id) not in response.text


def test_n04_every_provider_query_is_visible_and_revocation_is_immediate(
    platform: Platform,
) -> None:
    site = platform.site()
    ((_, zone_id),) = site.zones()
    _, client_admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    _, installer = platform.installer()
    granted = _grant(platform, installer, site.organization_id)
    assert granted.status_code == 201, granted.text
    concession = uuid.UUID(granted.json()["concession_id"])
    for _ in range(3):
        assert _coverage(platform, installer, zone_id, concession).status_code == 200
    # Ninguna consulta del proveedor es invisible para el cliente (BR-NUC-41).
    queries = platform.call("GET", f"/concessions/{concession}/queries", cookie=client_admin)
    assert queries.status_code == 200, queries.text
    listed = queries.json()["queries"]
    assert [(q["method"], q["resource"]) for q in listed] == [
        ("GET", "/zones/{zone_id}/coverage")
    ] * 3
    panel = platform.call("GET", "/concessions", cookie=client_admin)
    (shown,) = [c for c in panel.json()["concessions"] if c["concession_id"] == str(concession)]
    assert shown["reason"] == REASON and shown["status"] == "active"
    # El cliente revoca: la petición siguiente ya no ve nada.
    revoked = platform.call("POST", f"/concessions/{concession}/revoke", cookie=client_admin)
    assert revoked.status_code == 200, revoked.text
    after = _coverage(platform, installer, zone_id, concession)
    assert code_of(after) in ("not_found", "unauthenticated"), after.text
    # La sesión del proveedor sigue viva para su propia organización (BR-NUC-39).
    me = platform.call("GET", "/me", cookie=installer)
    assert me.status_code == 200 and me.json()["concession_id"] is None
    panel = platform.call("GET", "/concessions", cookie=client_admin)
    (shown,) = [c for c in panel.json()["concessions"] if c["concession_id"] == str(concession)]
    assert shown["status"] == "revoked"
    assert platform.records(site.organization_id, "provider_concession_revoked")
