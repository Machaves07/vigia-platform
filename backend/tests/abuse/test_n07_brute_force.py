"""N-7 · Fuerza bruta y relleno de credenciales (H-58; business-rules §14).

**Qué intenta**: probar contraseñas contra una cuenta, o correos y contraseñas filtrados contra
muchas, desde una o varias direcciones; o esquivar el límite por origen falsificando
``X-Forwarded-For``.

**Qué lo detiene** (BR-NUC-20 a BR-NUC-24, con BR-NUC-94):

- BR-NUC-20: contraseña verificada contra la lista de filtradas (también al cambiarla);
- BR-NUC-21 y BR-NUC-22: segundo factor obligatorio para las cuentas administrativas: la
  contraseña sola no da una sesión utilizable;
- BR-NUC-23: mensajes idénticos para correo desconocido y contraseña incorrecta;
- BR-NUC-24: retardo progresivo tras 5 fallos por cuenta **o** por origen en 15 minutos, también
  con intentos simultáneos (la reserva se serializa en la fila del contador); los intentos sobre
  cuentas inexistentes cuentan por origen; alerta a 10 fallos por cuenta;
- BR-NUC-94: límite por origen en las rutas públicas; el origen es la dirección de la conexión, y
  un ``X-Forwarded-For`` solo cuenta si lo pone el balanceador de confianza (seguimiento de la
  revisión de VIG-78; el arranque con ``--proxy-headers --forwarded-allow-ips`` limitado a las
  subredes del balanceador es de la imagen y de ``vigia-compute``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tests.hierarchy_support import REJECTED_PASSWORD, new_email
from tests.platform_support import Platform, code_of, comparable
from vigia_platform.shared.context import Role
from vigia_platform.shared.ratelimit import PUBLIC_ORIGIN_BUDGET

pytestmark = pytest.mark.integration

WRONG = "clave-equivocada-de-prueba"
BALANCER_SUBNET = "10.20.0.0/16"


def _login_body(email: str, password: str) -> dict[str, str]:
    return {"email": email, "password": password}


def test_n07_five_failures_on_an_account_hold_even_the_right_password(platform: Platform) -> None:
    site = platform.site()
    user = platform.account(site.organization_id, Role.COORDINATOR_SST)
    bodies = []
    for attempt in range(5):
        # Cada intento desde una dirección distinta: el retardo es de la cuenta.
        response, cookie = platform.login(user.email, WRONG, address=f"192.0.2.{10 + attempt}")
        assert response.status_code == 401 and cookie is None
        bodies.append(comparable(response))
    unknown, _ = platform.login(new_email("nadie"), WRONG, address="192.0.2.99")
    assert comparable(unknown) == bodies[0]  # BR-NUC-23
    held, cookie = platform.login(user.email, user.password, address="192.0.2.50")
    assert held.status_code == 429 and code_of(held) == "throttled" and cookie is None
    assert held.headers["retry-after"] == "30"
    platform.advance(31)
    ok, cookie = platform.login(user.email, user.password, address="192.0.2.50")
    assert ok.status_code == 200 and cookie is not None


def test_n07_concurrent_guesses_never_get_more_than_five_verifications(
    platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = platform.site()
    user = platform.account(site.organization_id, Role.COORDINATOR_SST)
    verified: list[str] = []
    original = platform.passwords.verify

    async def counting(password: str, encoded: str) -> Any:
        verified.append(password)
        return await original(password, encoded)

    monkeypatch.setattr(platform.passwords, "verify", counting)

    async def burst() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(
                    platform.send(
                        "POST",
                        "/auth/login",
                        json_body=_login_body(user.email, f"{WRONG}-{i}"),
                        address=f"192.0.2.{100 + i}",
                    )
                    for i in range(12)
                )
            )
        )

    responses = platform.run(burst())
    codes = sorted(str(code_of(r)) for r in responses)
    # Las reservas se serializan en la fila del contador: ninguna esquiva el retardo.
    assert codes.count("unauthenticated") == 5, codes
    assert codes.count("throttled") == 7, codes
    assert len(verified) == 5, verified


def test_n07_credential_stuffing_from_one_origin_is_held_by_origin(platform: Platform) -> None:
    address = "192.0.2.200"
    for _ in range(5):
        response, _ = platform.login(new_email("filtrado"), WRONG, address=address)
        assert code_of(response) == "unauthenticated"
    held, _ = platform.login(new_email("filtrado"), WRONG, address=address)
    assert held.status_code == 429 and code_of(held) == "throttled"
    # Otra dirección no hereda el retardo del origen.
    other, _ = platform.login(new_email("filtrado"), WRONG, address="192.0.2.201")
    assert code_of(other) == "unauthenticated"


def test_n07_ten_failures_on_an_account_raise_one_alert(platform: Platform) -> None:
    site = platform.site()
    user = platform.account(site.organization_id, Role.COORDINATOR_SST)
    failures = 0
    for attempt in range(40):
        response, _ = platform.login(user.email, WRONG, address=f"198.51.100.{attempt + 1}")
        if code_of(response) == "throttled":
            platform.advance(int(response.headers["retry-after"]))
            continue
        failures += 1
        if failures == 10:
            break
    alerts = platform.alerts(site.organization_id, "login_failures_account")
    assert len(alerts) == 1, alerts


def test_n07_a_breached_password_is_refused_when_set(platform: Platform) -> None:
    site = platform.site()
    user = platform.account(site.organization_id, Role.COORDINATOR_SST)
    _, cookie = platform.login(user.email, user.password)
    assert cookie is not None
    weak = platform.call(
        "POST",
        "/auth/password",
        cookie=cookie,
        json_body={"current_password": user.password, "new_password": REJECTED_PASSWORD},
    )
    assert weak.status_code == 400 and code_of(weak) == "invalid_request"
    # La filtrada no entra: la anterior sigue valiendo.
    assert platform.login(user.email, REJECTED_PASSWORD)[0].status_code == 401


def test_n07_a_stolen_administrator_password_alone_opens_nothing(platform: Platform) -> None:
    genesis = platform.genesis()
    response, pending = platform.login(genesis.admin_email, genesis.password)
    assert response.json() == {"status": "second_factor_enrollment_required"}
    assert pending is not None
    for method, path in (("GET", "/me"), ("GET", "/users"), ("GET", "/hierarchy")):
        denied = platform.call(method, path, cookie=pending)
        assert denied.status_code == 401 and code_of(denied) == "unauthenticated", path


# --- BR-NUC-94: el origen del límite y ``X-Forwarded-For`` (seguimiento de VIG-78) -------------


async def _public_burst(
    app: Any, client: str, forwarded: Sequence[str | None], token: str = "A" * 43
) -> list[int]:
    """Peticiones públicas (aceptación de una invitación inexistente) desde ``client``."""
    transport = httpx.ASGITransport(app=app, client=(client, 40_000))
    statuses: list[int] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as http:
        for value in forwarded:
            headers = {"Sec-Fetch-Site": "same-origin"}
            if value is not None:
                headers["X-Forwarded-For"] = value
            response = await http.post(
                f"/invitations/{token}/accept", json={"step": "begin"}, headers=headers
            )
            statuses.append(response.status_code)
    return statuses


def test_n07_a_forged_forwarded_for_does_not_change_the_bucket(platform: Platform) -> None:
    budget = PUBLIC_ORIGIN_BUDGET.limit
    forged = [f"203.0.113.{i % 250 + 1}" for i in range(budget + 1)]
    statuses = platform.run(_public_burst(platform.app, "192.0.2.150", forged))
    # La cabecera cambia en cada petición y no sirve: la 61.ª desde la misma conexión, limitada.
    assert statuses[:budget] == [404] * budget
    assert statuses[budget] == 429
    # Otra dirección real tiene su propio cubo.
    assert platform.run(_public_burst(platform.app, "192.0.2.151", [None])) == [404]


def test_n07_behind_the_trusted_balancer_each_client_has_its_own_bucket(
    platform: Platform,
) -> None:
    budget = PUBLIC_ORIGIN_BUDGET.limit
    behind = ProxyHeadersMiddleware(platform.app, trusted_hosts=[BALANCER_SUBNET])
    balancer = "10.20.3.4"
    first = platform.run(_public_burst(behind, balancer, ["203.0.113.70"] * (budget + 1)))
    assert first[budget] == 429
    # Otro cliente tras el mismo balanceador no comparte el cubo del primero.
    assert platform.run(_public_burst(behind, balancer, ["203.0.113.71"])) == [404]
    # Un cliente directo (fuera de la subred del balanceador) que se hace pasar por otro: su
    # cabecera no se cree; ni hereda el cubo agotado ni se libra del suyo.
    assert platform.run(_public_burst(behind, "198.51.100.77", ["203.0.113.70"])) == [404]
    spoofer = platform.run(
        _public_burst(behind, "198.51.100.78", [f"203.0.113.{i}" for i in range(budget + 1)])
    )
    assert spoofer[budget] == 429
