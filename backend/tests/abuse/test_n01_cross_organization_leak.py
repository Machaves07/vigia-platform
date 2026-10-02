"""N-1 · Fuga entre organizaciones (business-rules §14; SECURITY-11, RNF-SEG-11).

**Qué intenta**: leer o escribir un recurso de otra organización adivinando o conociendo su
identificador.

**Qué lo detiene** (BR-NUC-01, BR-NUC-02, BR-NUC-09):

- BR-NUC-01: seguridad a nivel de fila forzada; sin variable de sesión no se ve ninguna fila, y con
  la de otra organización, ninguna de esta;
- BR-NUC-02: toda transacción de datos exige ``ScopeContext``;
- BR-NUC-09: un recurso fuera de alcance responde **exactamente igual** que uno inexistente
  (``not_found``, nunca ``forbidden``), y una escritura por identificador conocido no cambia nada;
- BR-NUC-59 (NFR-NUC-28): cada denegación se audita y quien sondea en ráfaga dispara **una**
  alerta ``authorization_denied_repeated`` al cruzar el umbral.

El recorrido de **todas** las rutas con dos organizaciones es de ``tests/isolation`` (TASK-139);
aquí queda el escenario de ataque concreto, de extremo a extremo.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.factories import uuid7
from tests.identity_db import set_scope
from tests.platform_support import T0, Platform, code_of, comparable
from vigia_platform.identity.adapters.authz_store import DENIED_REPEATED_THRESHOLD
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration


def _victim(platform: Platform) -> tuple[Any, Any, Any]:
    """Organización B con un registro del expediente y una persona."""
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    record_id = platform.gate(site, plant_id, zone_id, T0)
    user_id, _ = platform.person(site.organization_id, Role.COPASST)
    return site, record_id, user_id


def test_n01_a_known_record_of_another_organization_reads_like_a_missing_one(
    platform: Platform,
) -> None:
    _, record_id, _ = _victim(platform)
    attacker = platform.site()
    _, cookie = platform.person(attacker.organization_id, Role.ADMINISTRATOR, Role.COORDINATOR_SST)
    foreign = platform.call("GET", f"/ledger/records/{record_id}", cookie=cookie)
    missing = platform.call("GET", f"/ledger/records/{uuid7()}", cookie=cookie)
    assert foreign.status_code == 404 and code_of(foreign) == "not_found"
    assert comparable(foreign) == comparable(missing)
    assert str(record_id) not in foreign.text


def test_n01_a_write_by_known_identifier_changes_nothing_in_the_other_organization(
    platform: Platform,
) -> None:
    victim, _, user_id = _victim(platform)
    attacker = platform.site()
    _, cookie = platform.person(attacker.organization_id, Role.ADMINISTRATOR)
    before = platform.fetch(
        "SELECT display_name, status FROM identity.user_account WHERE user_id = $1", user_id
    )
    for method, path, body in (
        ("PATCH", f"/users/{user_id}", {"display_name": "Nombre cambiado"}),
        ("POST", f"/users/{user_id}/deactivate", None),
        ("POST", f"/users/{user_id}/second-factor/reset", None),
    ):
        foreign = platform.call(method, path, cookie=cookie, json_body=body)
        missing = platform.call(
            method, path.replace(str(user_id), str(uuid7())), cookie=cookie, json_body=body
        )
        assert code_of(foreign) == "not_found", (path, foreign.text)
        assert comparable(foreign) == comparable(missing), path
    after = platform.fetch(
        "SELECT display_name, status FROM identity.user_account WHERE user_id = $1", user_id
    )
    assert after == before
    # El intento no deja rastro en la cadena de auditoría de B: ni siquiera se sabe allí.
    assert not platform.fetch(
        "SELECT 1 FROM shared.audit_entry WHERE organization_id = $1 AND actor_id <> $2",
        victim.organization_id,
        user_id,
    )


def test_n01_row_security_shows_nothing_without_context_and_nothing_of_b_with_a(
    platform: Platform,
) -> None:
    victim, record_id, _ = _victim(platform)
    attacker = platform.site()
    platform.gate(attacker, *attacker.zones()[0], T0)

    async def probe() -> tuple[int, int, set[Any]]:
        connection = await platform.connect_as("vigia_app")
        try:
            # Una consulta a la que «se le olvidó» el contexto: ninguna fila, no las de todos.
            async with connection.transaction():
                bare = await connection.fetchval("SELECT count(*) FROM ledger.ledger_record")
            # Con el contexto de A y el filtro de B escrito a mano: tampoco.
            async with connection.transaction():
                await set_scope(connection, attacker.organization_id)
                by_id = await connection.fetchval(
                    "SELECT count(*) FROM ledger.ledger_record WHERE record_id = $1", record_id
                )
                seen = {
                    row["organization_id"]
                    for row in await connection.fetch(
                        "SELECT organization_id FROM ledger.ledger_record"
                    )
                }
            return int(bare), int(by_id), seen
        finally:
            await connection.close()

    bare, by_id, seen = platform.run(probe())
    assert (bare, by_id) == (0, 0)
    assert seen == {attacker.organization_id}
    # Las filas existen: lo que las oculta es la política, no que falten.
    assert platform.records(victim.organization_id, "gate_state_changed")


def test_n01_thirty_concurrent_probes_raise_exactly_one_repeated_denial_alert(
    platform: Platform,
) -> None:
    """Quien sondea identificadores acumula denegaciones: la 21.ª en 10 minutos alerta una sola
    vez, también con las 30 a la vez (seguimiento 2 de la revisión de VIG-86, NFR-NUC-28)."""
    site = platform.site()
    # La administración no tiene ``findings.read``: cada lista de hallazgos es una denegación.
    prober, cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)

    async def probes() -> list[Any]:
        return list(
            await asyncio.gather(
                *(platform.send("GET", "/ledger/records", cookie=cookie) for _ in range(30))
            )
        )

    responses = platform.run(probes())
    assert [code_of(r) for r in responses] == ["not_found"] * 30
    assert len(platform.audit_entries(site.organization_id, "authorization_denied")) == 30
    alerts = platform.alerts(site.organization_id, "authorization_denied_repeated")
    assert [alert["resource_id"] for alert in alerts] == [str(prober)]


@pytest.mark.parametrize("burst", [DENIED_REPEATED_THRESHOLD, DENIED_REPEATED_THRESHOLD + 1])
@pytest.mark.parametrize("round_", range(3))
def test_n01_a_concurrent_burst_alerts_exactly_at_the_crossing(
    platform: Platform, burst: int, round_: int
) -> None:
    """La alerta sale en el cruce, no después: una ráfaga concurrente de exactamente umbral + 1
    denegaciones alerta una vez, y una de exactamente el umbral no alerta (NFR-NUC-28).

    Si la cuenta se leyera fuera de la exclusión de la cabeza de auditoría, las peticiones
    encoladas verían cuentas atrasadas y la 21.ª no vería 20 previas: la ráfaga de 21 quedaría
    sin alerta (revisión de PR #50, seguimiento 2 de VIG-86). Tres rondas por la carrera.
    """
    site = platform.site()
    prober, cookie = platform.person(site.organization_id, Role.ADMINISTRATOR)

    async def probes() -> list[Any]:
        return list(
            await asyncio.gather(
                *(platform.send("GET", "/ledger/records", cookie=cookie) for _ in range(burst))
            )
        )

    responses = platform.run(probes())
    assert [code_of(r) for r in responses] == ["not_found"] * burst
    assert len(platform.audit_entries(site.organization_id, "authorization_denied")) == burst
    alerts = platform.alerts(site.organization_id, "authorization_denied_repeated")
    expected = [str(prober)] if burst > DENIED_REPEATED_THRESHOLD else []
    assert [alert["resource_id"] for alert in alerts] == expected
