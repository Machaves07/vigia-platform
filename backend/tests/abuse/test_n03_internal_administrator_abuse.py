"""N-3 · Abuso interno del administrador (R4; business-rules §14).

**Qué intenta**: un administrador (o quien tenga sus credenciales de base) lee hallazgos, o altera
o borra registros del expediente.

**Qué lo detiene** (BR-NUC-15, BR-NUC-43, BR-NUC-46):

- BR-NUC-15: las prohibiciones por diseño no tienen clave de permiso; ``administrator`` no tiene
  ``findings.read`` y no existe ninguna ruta que modifique o borre un registro;
- BR-NUC-43: el rol de la aplicación solo tiene ``INSERT`` y ``SELECT`` sobre el expediente; un
  disparador rechaza ``UPDATE``, ``DELETE`` y ``TRUNCATE`` incluso para roles con más privilegios;
- BR-NUC-46 (con BR-NUC-56): los hashes encadenados delatan el registro alterado por quien se salta
  el disparador; la verificación lo publica como ``broken`` en ``GET /integrity/results`` y eleva
  ``integrity_compromised`` (seguimiento 1 de la revisión de VIG-86).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.identity_db import set_scope
from tests.ledger_database import INSUFFICIENT_PRIVILEGE, RESTRICT_VIOLATION
from tests.platform_support import HOUR, T0, Platform, code_of
from tests.verify_support import mutate
from tests.writer_support import unit_context
from vigia_platform.ledger.chain.checkpoints import CheckpointChain
from vigia_platform.ledger.chain.verify import VerificationMode
from vigia_platform.shared.context import ActorKind, ActorUnit, Role

pytestmark = pytest.mark.integration


def _chain(platform: Platform, records: int = 3) -> tuple[Any, Any, list[Any]]:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    ids = [platform.gate(site, plant_id, zone_id, T0 + i * HOUR) for i in range(records)]
    return site, plant_id, ids


def _row(platform: Platform, record_id: Any) -> dict[str, Any]:
    (row,) = platform.fetch(
        "SELECT content, content_hash, record_hash FROM ledger.ledger_record WHERE record_id = $1",
        record_id,
    )
    return dict(row)


def test_n03_the_administrator_reads_no_finding_and_has_no_route_to_alter_one(
    platform: Platform,
) -> None:
    site, _, (record_id, *_) = _chain(platform, 1)
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    before = _row(platform, record_id)
    read = platform.call("GET", f"/ledger/records/{record_id}", cookie=admin)
    assert read.status_code == 404 and code_of(read) == "not_found"
    for method in ("PATCH", "PUT", "DELETE"):
        response = platform.call(
            method,
            f"/ledger/records/{record_id}",
            cookie=admin,
            json_body={"content": {"resulting_mode": "commissioning"}},
        )
        assert response.status_code in (404, 405), (method, response.text)
        assert "resulting_mode" not in response.text
    assert _row(platform, record_id) == before


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE", "TRUNCATE"])
def test_n03_the_database_rejects_rewrites_for_the_application_and_privileged_roles(
    platform: Platform, statement: str
) -> None:
    site, _, (record_id, *_) = _chain(platform, 1)
    before = _row(platform, record_id)
    sql = {
        "UPDATE": "UPDATE ledger.ledger_record SET correlation_id = record_id WHERE record_id = $1",
        "DELETE": "DELETE FROM ledger.ledger_record WHERE record_id = $1",
        "TRUNCATE": "TRUNCATE ledger.ledger_record CASCADE",
    }[statement]
    arguments = () if statement == "TRUNCATE" else (record_id,)

    async def attempt(role: str | None) -> str | None:
        connection = await platform.connect_as(role)
        try:
            async with connection.transaction():
                await set_scope(connection, site.organization_id)
                await connection.execute(sql, *arguments)
        except Exception as error:
            return str(getattr(error, "sqlstate", type(error).__name__))
        finally:
            await connection.close()
        return None

    # La aplicación: sin el permiso. El dueño de las tablas y el superusuario: el disparador.
    assert platform.run(attempt("vigia_app")) == INSUFFICIENT_PRIVILEGE
    assert platform.run(attempt("vigia_migrate")) == RESTRICT_VIOLATION
    assert platform.run(attempt(None)) == RESTRICT_VIOLATION
    assert _row(platform, record_id) == before


def test_n03_an_altered_record_breaks_the_chain_and_is_shown_with_its_alert(
    platform: Platform,
) -> None:
    site, plant_id, ids = _chain(platform, 3)
    organization_id = site.organization_id
    # Quien controla la base se salta el disparador y cambia el contenido del segundo registro.
    altered = json.loads(bytes(_row(platform, ids[1])["content"]))
    altered["resulting_mode"] = "commissioning"
    canonical = json.dumps(altered, sort_keys=True, separators=(",", ":")).encode()
    platform.run(
        mutate(
            platform.authz.sessions.migrated,
            "ledger.ledger_record",
            "record_id",
            ids[1],
            {"content": canonical},
        )
    )
    context = unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
    result = platform.run(
        platform.verifier.verify(
            context, CheckpointChain.plant(plant_id), VerificationMode.ON_DEMAND
        )
    )
    assert not result.intact
    assert (result.broken_sequence, result.broken_entry_id) == (2, ids[1])
    # GET /integrity/results muestra la cadena rota con el punto exacto; no la oculta.
    _, coordinator = platform.person(organization_id, Role.COORDINATOR_SST)
    listed = platform.call("GET", "/integrity/results", cookie=coordinator)
    assert listed.status_code == 200, listed.text
    (shown,) = [r for r in listed.json()["results"] if r["chain"]["plant_id"] == str(plant_id)]
    assert (shown["result"], shown["broken_sequence"], shown["broken_entry_id"]) == (
        "broken",
        2,
        str(ids[1]),
    )
    # …y la alerta de máxima severidad, una por verificación rota.
    (alert,) = [
        json.loads(row["payload"])
        for row in platform.events(organization_id, "integrity_compromised")
    ]
    assert alert["first_failed_sequence"] == 2 and alert["chain_kind"] == "ledger"
    # La verificación queda en la auditoría (BR-NUC-56) y lo escrito no se «repara» (BR-NUC-58).
    audits = platform.audit_entries(organization_id, "integrity_verification")
    assert json.loads(bytes(audits[-1]["filters"]))["result"] == "broken"
    assert json.loads(bytes(_row(platform, ids[1])["content"]))["resulting_mode"] == (
        "commissioning"
    )
