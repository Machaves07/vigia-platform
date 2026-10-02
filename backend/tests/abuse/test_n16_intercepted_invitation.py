"""N-16 · Invitación interceptada (H-58; business-rules §14).

**Qué intenta**: activar una cuenta ajena con un enlace de invitación robado: reutilizarlo tras la
activación legítima, usarlo días después, usar uno que ya se sustituyó o se canceló (también si
la cuenta se desactivó y se reactivó después), ganarle la carrera a la persona invitada, o
activar una cuenta administrativa sin segundo factor.

**Qué lo detiene** (BR-NUC-30, BR-NUC-32, con BR-NUC-33):

- BR-NUC-30: token de ≥ 128 bits, de **un solo uso**, guardado como hash, válido 72 horas; una
  invitación nueva cancela la anterior;
- BR-NUC-32: la activación inscribe el segundo factor si el rol lo exige; la divulgación del
  enlace al administrador se audita (``invitation_link_disclosed``) sin el enlace;
- BR-NUC-33: desactivar cancela las invitaciones pendientes y reactivar emite una nueva: la
  cancelada no revive (seguimiento 1 de la revisión de VIG-82).

Todas las formas de un token que no vale responden lo mismo (``not_found``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest

from tests.hierarchy_support import GOOD_PASSWORD, new_email
from tests.platform_support import Platform, code_of, comparable
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

COMPLETE: dict[str, Any] = {
    "step": "complete",
    "password": GOOD_PASSWORD,
    "notice_version": CURRENT_PRIVACY_NOTICE_VERSION,
}


def _invite(
    platform: Platform, admin: Any, plant_id: uuid.UUID, role: Role = Role.COORDINATOR_SST
) -> tuple[uuid.UUID, str]:
    level = ScopeLevel.ORGANIZATION if role is Role.ADMINISTRATOR else ScopeLevel.PLANT
    scope = None if role is Role.ADMINISTRATOR else plant_id
    organization = platform.fetch(
        "SELECT organization_id FROM identity.plant WHERE plant_id = $1", plant_id
    )[0]["organization_id"]
    response = platform.call(
        "POST",
        "/users",
        cookie=admin,
        json_body={
            "email": new_email("invitada"),
            "display_name": "Persona sintética",
            "assignments": [
                {
                    "role": role.value,
                    "scope_level": level.value,
                    "scope_id": str(scope or organization),
                }
            ],
            "disclose_link": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["user_id"]), body["link"].split("#", 1)[1]


def _accept(platform: Platform, token: str, body: dict[str, Any]) -> httpx.Response:
    return platform.call("POST", f"/invitations/{token}/accept", json_body=body)


def _status(platform: Platform, user_id: uuid.UUID) -> str:
    (row,) = platform.fetch("SELECT status FROM identity.user_account WHERE user_id = $1", user_id)
    status: str = row["status"]
    return status


def _setup(platform: Platform) -> tuple[Any, uuid.UUID, Any]:
    site = platform.site()
    ((plant_id, _),) = site.zones()
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    return site, plant_id, admin


def test_n16_a_used_or_expired_link_answers_like_a_missing_one(platform: Platform) -> None:
    _, plant_id, admin = _setup(platform)
    user_id, used = _invite(platform, admin, plant_id)
    assert _accept(platform, used, COMPLETE).json() == {"status": "activated"}
    assert _status(platform, user_id) == "active"
    _, expired = _invite(platform, admin, plant_id)
    platform.advance(timedelta(hours=72).total_seconds())
    missing = _accept(platform, "A" * 43, {"step": "begin"})
    assert code_of(missing) == "not_found"
    for token in (used, expired):
        for body in ({"step": "begin"}, COMPLETE):
            response = _accept(platform, token, body)
            assert comparable(response) == comparable(missing), (token, body)


def test_n16_the_token_is_kept_only_as_a_hash_and_its_disclosure_is_audited(
    platform: Platform,
) -> None:
    site, plant_id, admin = _setup(platform)
    user_id, token = _invite(platform, admin, plant_id)
    for table in ("identity.invitation", "shared.audit_entry", "shared.outbox_event"):
        dump = platform.fetch(
            f"SELECT to_jsonb(t)::text AS row FROM {table} AS t"  # noqa: S608 - nombre fijo
            " WHERE organization_id = $1",
            site.organization_id,
        )
        assert dump or table != "identity.invitation"
        assert all(token not in row["row"] for row in dump), table
    assert all(token not in row["content_json"] for row in platform.records(site.organization_id))
    (invitation,) = platform.fetch(
        "SELECT invitation_id FROM identity.invitation WHERE user_id = $1", user_id
    )
    (disclosed,) = [
        row
        for row in platform.audit_entries(site.organization_id, "invitation_link_disclosed")
        if row["resource_id"] == invitation["invitation_id"]
    ]
    assert json.loads(bytes(disclosed["filters"])) == {"user_id": str(user_id)}


def test_n16_a_replaced_or_cancelled_link_never_revives(platform: Platform) -> None:
    _, plant_id, admin = _setup(platform)
    user_id, cancelled = _invite(platform, admin, plant_id)
    # Desactivar cancela la invitación pendiente; reactivar emite otra y vuelve a ``invited``.
    assert platform.call("POST", f"/users/{user_id}/deactivate", cookie=admin).status_code == 200
    reactivated = platform.call(
        "POST",
        f"/users/{user_id}/reactivate",
        cookie=admin,
        json_body={
            "assignments": [
                {"role": "coordinator_sst", "scope_level": "plant", "scope_id": str(plant_id)}
            ],
            "disclose_link": True,
        },
    )
    assert reactivated.status_code == 200, reactivated.text
    fresh = reactivated.json()["link"].split("#", 1)[1]
    assert _status(platform, user_id) == "invited"
    # El enlace cancelado no sirve aunque la cuenta vuelva a estar invitada…
    for body in ({"step": "begin"}, COMPLETE):
        stale = _accept(platform, cancelled, body)
        assert stale.status_code == 404 and code_of(stale) == "not_found", stale.text
    assert _status(platform, user_id) == "invited"
    # …y el nuevo sí.
    assert _accept(platform, fresh, COMPLETE).json() == {"status": "activated"}


def test_n16_an_administrator_invitation_cannot_be_activated_without_second_factor(
    platform: Platform,
) -> None:
    _, plant_id, admin = _setup(platform)
    user_id, token = _invite(platform, admin, plant_id, Role.ADMINISTRATOR)
    begin = _accept(platform, token, {"step": "begin"})
    assert begin.status_code == 200 and begin.json()["second_factor_required"] is True
    for code in (None, "000000"):
        body = dict(COMPLETE) if code is None else {**COMPLETE, "second_factor_code": code}
        refused = _accept(platform, token, body)
        assert refused.status_code in (400, 401), refused.text
        assert _status(platform, user_id) == "invited"
    activated = _accept(platform, token, {**COMPLETE, "second_factor_code": "246810"})
    assert activated.json() == {"status": "activated"}


@pytest.mark.parametrize("rounds", range(3))
def test_n16_two_simultaneous_activations_of_one_link_let_only_one_in(
    platform: Platform, rounds: int
) -> None:
    _, plant_id, admin = _setup(platform)
    user_id, token = _invite(platform, admin, plant_id)

    async def race() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                platform.send("POST", f"/invitations/{token}/accept", json_body=COMPLETE),
                platform.send(
                    "POST",
                    f"/invitations/{token}/accept",
                    json_body={**COMPLETE, "password": "clave-del-atacante-larga"},
                ),
            )
        )

    responses = platform.run(race())
    outcomes = sorted(code_of(r) or r.json()["status"] for r in responses)
    assert outcomes == ["activated", "not_found"], [r.text for r in responses]
    assert _status(platform, user_id) == "active"
    used = platform.fetch("SELECT status FROM identity.invitation WHERE user_id = $1", user_id)
    assert [row["status"] for row in used] == ["accepted"]
    # Una sola contraseña quedó: la del ganador.
    winner = next(
        COMPLETE["password"] if i == 0 else "clave-del-atacante-larga"
        for i, r in enumerate(responses)
        if r.status_code == 200
    )
    (credential,) = platform.fetch(
        "SELECT password_hash FROM identity.password_credential WHERE user_id = $1", user_id
    )
    assert credential["password_hash"] == f"fake${winner}"
