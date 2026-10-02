"""N-14 · Identidad de una persona colada en un texto (P3; business-rules §14).

**Qué intenta**: escribir un nombre, un documento, un correo o un teléfono en un campo de texto
libre (el nombre de la organización, de una planta o una zona, el nombre visible de una cuenta,
el motivo de una concesión), disfrazado con caracteres de ancho completo, espacios especiales,
invisibles o marcado.

**Qué lo detiene** (BR-NUC-44, BR-NUC-51):

- BR-NUC-44: toda ruta de texto libre pasa ``FreeTextPolicy`` antes de escribirse: la política
  base de U-02 (NFC, sin controles, invisibles ni marcado, longitudes) y los validadores que
  registran las unidades (el de U-04 para datos de personas, A-45), que ven la **forma canónica**
  (NFKC, espacios Unicode como espacio, sin diacríticos, minúsculas);
- BR-NUC-51: el texto libre solo existe en rutas declaradas (la metapropiedad lo comprueba al
  registrar los tipos; ver N-11).

Seguimiento de la revisión de VIG-83: el nombre de la organización pasa por la política vigente
(una prueba aquí falla si se le quita). El validador de datos de contacto que se registra en
estas pruebas es un doble del de U-04, que aún no existe: con la política de hoy, sin él, un
correo en el nombre **se acepta** (lo fija ``test_n14_today_without_the_u04_validator…``).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.hierarchy_support import new_code
from tests.platform_support import CONTACT_VALIDATOR, Platform, code_of
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

FULLWIDTH_DIGITS = "".join(chr(0xFF10 + int(d)) for d in "3001234567")
NBSP = chr(0x00A0)
ZERO_WIDTH = chr(0x200B)
IDENTIFYING = {
    "correo": "Planta de juan.perez@example.test",
    "telefono": "Turno noche +57 300 123 4567",
    "ancho-completo": f"Llamar al {FULLWIDTH_DIGITS}",
    "nbsp": f"Llamar al 300{NBSP}123{NBSP}4567",
    "mayusculas": "Contacto JUAN.PEREZ@EXAMPLE.TEST",
}
DISGUISED = {
    "invisible": f"Planta{ZERO_WIDTH}norte",
    "marcado": "Planta <b>norte</b>",
    "entidad": "Planta &lt;norte",
    "control": "Planta\nnorte",
}


def _organization_name(platform: Platform, cookie: Any) -> str:
    response = platform.call("GET", "/organization/settings", cookie=cookie)
    assert response.status_code == 200, response.text
    name: str = response.json()["name"]
    return name


@pytest.mark.parametrize("text", [*IDENTIFYING.values(), *DISGUISED.values()],
                         ids=[*IDENTIFYING, *DISGUISED])  # fmt: skip
def test_n14_the_organization_name_goes_through_the_free_text_policy(
    platform: Platform, text: str
) -> None:
    site = platform.site()
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    before = _organization_name(platform, admin)
    response = platform.call(
        "PATCH", "/organization/settings", cookie=admin, json_body={"name": text}
    )
    assert response.status_code == 400, response.text
    assert code_of(response) == "invalid_request"
    assert text not in response.text
    assert _organization_name(platform, admin) == before
    # Un nombre corriente sí se acepta (sin esto, la prueba pasaría con una ruta rota).
    fine = platform.call(
        "PATCH", "/organization/settings", cookie=admin, json_body={"name": "Planta Norte S.A."}
    )
    assert fine.status_code == 200, fine.text
    assert _organization_name(platform, admin) == "Planta Norte S.A."


@pytest.mark.parametrize("text", list(IDENTIFYING.values()), ids=list(IDENTIFYING))
def test_n14_names_and_reasons_elsewhere_refuse_contact_data(platform: Platform, text: str) -> None:
    site = platform.site()
    ((plant_id, _),) = site.zones()
    _, admin = platform.person(site.organization_id, Role.ADMINISTRATOR)
    target, _ = platform.person(site.organization_id, Role.COPASST)
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        (
            "POST",
            "/plants",
            {
                "code": new_code("PL"),
                "name": text,
                "country": "CO",
                "data_region": "us-east-1",
                "timezone": "America/Bogota",
            },
        ),
        ("POST", f"/plants/{plant_id}/zones", {"code": new_code("ZN"), "name": text}),
        ("PATCH", f"/users/{target}", {"display_name": text}),
    ]
    for method, path, body in attempts:
        response = platform.call(method, path, cookie=admin, json_body=body)
        assert response.status_code == 400 and code_of(response) == "invalid_request", (
            path,
            response.text,
        )
    _, installer = platform.installer()
    reason = platform.call(
        "POST",
        "/provider/concessions",
        cookie=installer,
        json_body={
            "client_organization_id": str(site.organization_id),
            "scope_level": "organization",
            "scope_id": str(site.organization_id),
            "reason": f"Soporte pedido por {text}",
        },
    )
    assert reason.status_code == 400 and code_of(reason) == "invalid_request", reason.text
    # Nada de eso llegó a la base ni al expediente.
    for table in ("identity.plant", "identity.zone", "identity.user_account"):
        dump = platform.fetch(
            f"SELECT to_jsonb(t)::text AS row FROM {table} AS t"  # noqa: S608 - nombre fijo
            " WHERE organization_id = $1",
            site.organization_id,
        )
        assert all(text not in row["row"] for row in dump), table
    assert all(text not in row["content_json"] for row in platform.records(site.organization_id))


def test_n14_today_without_the_u04_validator_the_base_policy_alone_lets_contact_data_in() -> None:
    """La política vigente sin validadores enchufados: el correo pasa (pendiente de U-04, A-45).

    Si esta prueba falla es porque la política base ya detecta datos de contacto: entonces el
    doble de ``platform_support`` sobra y el docstring del módulo debe cambiar.
    """
    base = FreeTextPolicyRegistry()
    field = FreeTextField("organization", "/name", 1, 200)
    assert base.apply(IDENTIFYING["correo"], field) == IDENTIFYING["correo"]
    assert base.validator_names == ()
    plugged = FreeTextPolicyRegistry()
    plugged.register(CONTACT_VALIDATOR, lambda candidate, field: None)
    assert plugged.validator_names == (CONTACT_VALIDATOR,)
