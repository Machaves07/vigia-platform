"""N-8 · Robo de la cookie de sesión (H-58; business-rules §14).

**Qué intenta**: reutilizar desde otro equipo una sesión robada: leer la cookie desde la página,
llevarla en una URL, seguir usándola horas después o tras el cambio de contraseña.

**Qué lo detiene** (BR-NUC-25 a BR-NUC-27):

- BR-NUC-25: identificador aleatorio guardado solo como hash; cookie ``__Host-`` ``Secure``,
  ``HttpOnly`` y ``SameSite=Strict``, nunca en una URL; vence a los 30 minutos sin actividad y a
  las 12 horas en todo caso; se valida en **cada** petición;
- BR-NUC-26: la persona ve sus sesiones y cierra las demás; cambiar la contraseña cierra todas
  salvo la actual. Tras cerrar las demás, la lista solo muestra la actual (seguimiento 2 de la
  revisión de VIG-82);
- BR-NUC-27: el cierre invalida en el servidor y queda auditado con su motivo.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.platform_support import Platform, code_of
from tests.session_support import User
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

NEW_PASSWORD = "otra-clave-sintetica-larga"  # noqa: S105 - dato sintético


def _usable(platform: Platform, cookie: SessionCookie | None) -> bool:
    return bool(platform.call("GET", "/me", cookie=cookie).status_code == 200)


def _person(platform: Platform) -> User:
    site = platform.site()
    return platform.account(site.organization_id, Role.COORDINATOR_SST)


def test_n08_the_cookie_is_out_of_reach_of_scripts_and_never_travels_in_a_url(
    platform: Platform,
) -> None:
    user = _person(platform)
    response, cookie = platform.login(user.email, user.password)
    assert cookie is not None
    (header,) = [
        h for h in response.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE_NAME)
    ]
    assert SESSION_COOKIE_NAME.startswith("__Host-")
    attributes = {part.strip() for part in header.split(";")[1:]}
    assert {"Secure", "HttpOnly", "SameSite=Strict", "Path=/"} <= attributes
    assert not any(a.lower().startswith("domain") for a in attributes)
    # En la URL no vale: ni como parámetro ni con el nombre de la cookie.
    for params in ({"session": cookie.value}, {SESSION_COOKIE_NAME: cookie.value}):
        response = platform.call("GET", "/me", params=params)
        assert response.status_code == 401 and code_of(response) == "unauthenticated"
    # En la base solo está el hash del identificador.
    rows = platform.fetch(
        "SELECT session_id_hash FROM identity.session WHERE user_id = $1",
        user.user_id,
    )
    assert [row["session_id_hash"] for row in rows] == [cookie.session_id_hash]
    assert cookie.token not in {row["session_id_hash"] for row in rows}


def test_n08_a_stolen_cookie_dies_after_30_minutes_idle_and_after_12_hours_anyway(
    platform: Platform,
) -> None:
    user = _person(platform)
    _, idle = platform.login(user.email, user.password)
    _, active = platform.login(user.email, user.password)
    assert idle is not None and active is not None
    # La víctima sigue usando ``active`` cada 25 minutos; el ladrón guarda ``idle`` sin usarla.
    step = timedelta(minutes=25).total_seconds()
    platform.advance(step)
    assert _usable(platform, active)
    platform.advance(step)
    assert _usable(platform, active)
    # 50 minutos sin usarla: vencida por inactividad (comprobarla antes la habría prolongado).
    assert not _usable(platform, idle)
    for _ in range(26):
        platform.advance(step)
        assert _usable(platform, active)
    platform.advance(step)
    # 12 h 5 min desde el inicio: ni con actividad.
    assert not _usable(platform, active)


def test_n08_closing_the_other_sessions_kills_the_stolen_one_and_lists_only_the_current(
    platform: Platform,
) -> None:
    user = _person(platform)
    _, mine = platform.login(user.email, user.password)
    _, stolen = platform.login(user.email, user.password)
    _, third = platform.login(user.email, user.password)
    listed = platform.call("GET", "/auth/sessions", cookie=mine).json()["sessions"]
    assert len(listed) == 3
    closed = platform.call("POST", "/auth/sessions/close-others", cookie=mine)
    assert closed.status_code == 200 and closed.json() == {"sessions_closed": 2}
    assert not _usable(platform, stolen) and not _usable(platform, third)
    assert _usable(platform, mine)
    # La lista ya no muestra las cerradas: solo la actual.
    listed = platform.call("GET", "/auth/sessions", cookie=mine).json()["sessions"]
    assert [(s["current"], s["usable"]) for s in listed] == [(True, True)]
    # El cierre queda auditado con su motivo y cuántas cerró (BR-NUC-27).
    (end,) = platform.audit_entries(user.organization_id, "sessions_closed_others")
    assert end["result_count"] == 2
    assert json.loads(bytes(end["filters"])) == {"end_reason": "closed_by_user"}


def test_n08_a_password_change_closes_every_other_session(platform: Platform) -> None:
    user = _person(platform)
    _, mine = platform.login(user.email, user.password)
    _, stolen = platform.login(user.email, user.password)
    changed = platform.call(
        "POST",
        "/auth/password",
        cookie=mine,
        json_body={"current_password": user.password, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200 and changed.json() == {"sessions_closed": 1}
    assert not _usable(platform, stolen) and _usable(platform, mine)


def test_n08_logout_invalidates_on_the_server(platform: Platform) -> None:
    user = _person(platform)
    _, cookie = platform.login(user.email, user.password)
    assert cookie is not None
    assert platform.call("POST", "/auth/logout", cookie=cookie).status_code == 204
    # El ladrón que copió la cookie antes del cierre ya no entra.
    assert not _usable(platform, cookie)
