"""N-9 · Token de vista en vivo reutilizado o forjado (H-17, H-55; business-rules §14).

**Qué intenta**: usar un token de vista en vivo para ver otra zona (otro nodo), después de que
venza, con la firma cambiada, o acumular tokens; o, con ráfagas propias, dejar sin vista a los
demás de la organización.

**Qué lo detiene** (BR-NUC-88 a BR-NUC-90):

- BR-NUC-88: token de 10 minutos con ``aud`` = el nodo vigente de la zona, firmado con la clave de
  propósito ``live_view_token``; se audita su emisión (``jti``) y el token completo no se guarda;
- BR-NUC-89: un acceso local con un ``jti`` no emitido, o emitido para otro nodo, produce
  ``unknown_token_reported`` y ``security_alert``;
- BR-NUC-90: 30 emisiones por usuario cada 10 minutos; la exclusión es **por usuario**, así que la
  ráfaga de una persona no deja a las demás con ``rate_limited`` (seguimiento 3 de la revisión de
  VIG-86).
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest

from tests.factories import uuid7
from tests.live_view_support import jws_parts, node_verifies
from tests.platform_support import Platform, code_of
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration


def _zone_pair(platform: Platform) -> tuple[Any, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Una planta con dos zonas, cada una con su nodo."""
    site = platform.site(plants=1, zones_per_plant=2)
    (plant_id, first), (_, second) = site.zones()
    return (
        site,
        first,
        platform.node(site, plant_id, first),
        second,
        platform.node(site, plant_id, second),
    )


def _issue(platform: Platform, cookie: Any, zone_id: uuid.UUID) -> str:
    response = platform.call("POST", f"/zones/{zone_id}/live-view-token", cookie=cookie)
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


def _b64url(document: dict[str, Any]) -> str:
    raw = json.dumps(document, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_n09_a_token_opens_only_its_node_and_only_for_ten_minutes(platform: Platform) -> None:
    site, zone, node, _, other_node = _zone_pair(platform)
    _, copasst = platform.person(
        site.organization_id, Role.COPASST, level=ScopeLevel.ZONE, scope_id=zone
    )
    token = _issue(platform, copasst, zone)
    keys = platform.env.node_key_set()
    now = platform.clock.now()
    assert node_verifies(token, node, keys, now) is not None
    # El nodo de otra zona lo rechaza (``aud``), y el propio, al vencer.
    assert node_verifies(token, other_node, keys, now) is None
    assert node_verifies(token, node, keys, now + timedelta(seconds=599)) is not None
    assert node_verifies(token, node, keys, now + timedelta(seconds=600)) is None


def test_n09_a_forged_token_does_not_verify(platform: Platform) -> None:
    site, zone, node, other_zone, other_node = _zone_pair(platform)
    _, coordinator = platform.person(site.organization_id, Role.COORDINATOR_SST)
    token = _issue(platform, coordinator, zone)
    header, claims, signature = jws_parts(token)
    keys = platform.env.node_key_set()
    now = platform.clock.now()
    # Cambiar la audiencia, la zona o el vencimiento sin la clave privada rompe la firma.
    for change in (
        {"aud": str(other_node)},
        {"zone_id": str(other_zone)},
        {"exp": claims["exp"] + 3600},
    ):
        forged = ".".join(
            (
                _b64url(header),
                _b64url({**claims, **change}),
                base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
            )
        )
        assert node_verifies(forged, node, keys, now) is None, change
        assert node_verifies(forged, other_node, keys, now) is None, change
    # Otra clave (p. ej. la de catálogo) no es de propósito ``live_view_token``.
    assert node_verifies(".".join(token.split(".")[:2]) + ".AAAA", node, keys, now) is None


def test_n09_the_issuance_is_audited_by_jti_and_the_token_is_never_stored(
    platform: Platform,
) -> None:
    site, zone, _, _, _ = _zone_pair(platform)
    _, coordinator = platform.person(site.organization_id, Role.COORDINATOR_SST)
    token = _issue(platform, coordinator, zone)
    _, claims, _ = jws_parts(token)
    entries = platform.audit_entries(site.organization_id, "live_view_token_issued")
    assert [str(e["resource_id"]) for e in entries] == [claims["jti"]]
    assert json.loads(bytes(entries[0]["filters"]))["node_id"] == claims["aud"]
    signature = token.rsplit(".", 1)[1]
    for table in ("shared.audit_entry", "identity.live_view_token_issuance"):
        dump = platform.fetch(
            f"SELECT to_jsonb(t)::text AS row FROM {table} AS t"  # noqa: S608 - nombre fijo
            " WHERE organization_id = $1",
            site.organization_id,
        )
        assert all(token not in r["row"] and signature not in r["row"] for r in dump), table


def test_n09_a_local_access_with_an_unknown_or_foreign_jti_raises_an_alert(
    platform: Platform,
) -> None:
    site, zone, node, _, other_node = _zone_pair(platform)
    _, coordinator = platform.person(site.organization_id, Role.COORDINATOR_SST)
    _, claims, _ = jws_parts(_issue(platform, coordinator, zone))

    def access(jti: str) -> dict[str, Any]:
        return {
            "access_id": str(uuid7()),
            "jti": jti,
            "sub": claims["sub"],
            "role": claims["role"],
            "zone_id": claims["zone_id"],
            "opened_at": "2026-09-30T09:01:00.000Z",
            "closed_at": "2026-09-30T09:05:00.000Z",
            "outcome": "closed_expired",
        }

    service = platform.env.service()
    context = platform.env.node_context(site.organization_id)
    before = len(platform.events(site.organization_id, "security_alert"))
    # Un ``jti`` nunca emitido, y uno emitido para el nodo de otra zona.
    unknown = platform.run(service.incorporate(context, node, [access(str(uuid7()))]))
    foreign = platform.run(service.incorporate(context, other_node, [access(claims["jti"])]))
    assert (unknown.unknown, foreign.unknown) == (1, 1)
    assert len(platform.audit_entries(site.organization_id, "unknown_token_reported")) == 2
    assert len(platform.events(site.organization_id, "security_alert")) == before + 2


def test_n09_the_thirty_first_token_in_ten_minutes_is_refused(platform: Platform) -> None:
    site, zone, _, _, _ = _zone_pair(platform)
    _, coordinator = platform.person(site.organization_id, Role.COORDINATOR_SST)
    for _ in range(30):
        _issue(platform, coordinator, zone)
    refused = platform.call("POST", f"/zones/{zone}/live-view-token", cookie=coordinator)
    assert refused.status_code == 429 and code_of(refused) == "rate_limited"
    assert 1 <= refused.json()["retry_after_seconds"] <= 600


def test_n09_one_persons_burst_never_rate_limits_another_person(platform: Platform) -> None:
    site, zone, _, _, _ = _zone_pair(platform)
    _, noisy = platform.person(site.organization_id, Role.COORDINATOR_SST)
    others = [platform.person(site.organization_id, Role.COORDINATOR_SST)[1] for _ in range(3)]

    async def burst() -> tuple[list[httpx.Response], list[httpx.Response]]:
        path = f"/zones/{zone}/live-view-token"
        noisy_calls = [platform.send("POST", path, cookie=noisy) for _ in range(25)]
        other_calls = [platform.send("POST", path, cookie=cookie) for cookie in others]
        responses = await asyncio.gather(*noisy_calls, *other_calls)
        return list(responses[:25]), list(responses[25:])

    noisy_responses, other_responses = platform.run(burst())
    # La ráfaga de una persona espera en **su** exclusión; las demás no la comparten.
    assert [r.status_code for r in other_responses] == [200] * len(others), [
        r.text for r in other_responses
    ]
    assert all(r.status_code in (200, 429) for r in noisy_responses)
    issued = platform.audit_entries(site.organization_id, "live_view_token_issued")
    assert len(issued) == sum(r.status_code == 200 for r in noisy_responses) + len(others)


def test_n09_a_slow_issuance_holds_only_its_own_person(
    platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una emisión que tarda más que ``lock_timeout`` (2 s) con la exclusión tomada: la siguiente
    de la **misma** persona responde ``rate_limited``; la de otra persona sale enseguida."""
    site, zone, _, _, _ = _zone_pair(platform)
    slow_id, slow = platform.person(site.organization_id, Role.COORDINATOR_SST)
    _, other = platform.person(site.organization_id, Role.COORDINATOR_SST)
    audit = platform.authz.sessions.audit
    original = audit.append

    async def delayed(context: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
        if operation == AuditOperation.LIVE_VIEW_TOKEN_ISSUED and context.actor.id == slow_id:
            await asyncio.sleep(2.5)  # dentro de la transacción, con la exclusión tomada
        return await original(context, operation, *args, **kwargs)

    monkeypatch.setattr(audit, "append", delayed)
    path = f"/zones/{zone}/live-view-token"

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        first = asyncio.ensure_future(platform.send("POST", path, cookie=slow))
        await asyncio.sleep(0.3)  # la primera ya tiene la exclusión de su persona
        again, foreign = await asyncio.gather(
            platform.send("POST", path, cookie=slow), platform.send("POST", path, cookie=other)
        )
        return await first, again, foreign

    first, again, foreign = platform.run(scenario())
    assert first.status_code == 200, first.text
    assert again.status_code == 429 and code_of(again) == "rate_limited", again.text
    assert again.json()["retry_after_seconds"] == 1
    assert foreign.status_code == 200, foreign.text
