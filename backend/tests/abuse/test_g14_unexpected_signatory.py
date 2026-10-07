"""G-14 · Firmante que confirma un acuerdo del que no es firmante esperado (§12; H-50).

**Qué intenta**: completar las firmas del acuerdo de uso con quien no corresponde (otra persona
de la planta, la administración) o armar un acuerdo sin la representación de los trabajadores.

**Qué lo detiene**:

- BR-GOB-25: la política de firmantes exige ``minimum`` de 3 o más y el rol ``copasst``: si no,
  ``catalog_fewer_than_three`` o ``catalog_workers_representation_missing``, y lo mismo al crear un
  acuerdo con menos firmantes o sin ``copasst``; nada se escribe;
- BR-GOB-26: cada firmante confirma en su **propia** sesión; no hay ruta para confirmar por otro;
- BR-GOB-27: quien no es firmante esperado recibe ``catalog_signatory_not_expected`` y no queda
  confirmación; el ``copasst`` confirma con ``transparency.read`` (``origin = transparency``), sin
  ningún permiso de gestión; sin todas las firmas, la aprobación es
  ``catalog_signatures_incomplete`` y la zona no pasa a ``productive``.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, Signatory, detail_of, ok
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration


def _ready(flow: Onboarding) -> GobZone:
    """Zona montada, con acta cerrada y política de planta: solo faltan las firmas."""
    zone = flow.zone()
    flow.mount(zone)
    flow.closed_record(zone)
    flow.plant_policy(zone)
    return zone


def _confirmations(gob: GobPlatform, agreement_id: str) -> list[Any]:
    return gob.fetch(
        "SELECT user_id, role_in_use, origin FROM catalog.agreement_confirmation"
        " WHERE agreement_id = $1 ORDER BY confirmed_at",
        uuid.UUID(agreement_id),
    )


def test_g14_a_policy_or_an_agreement_without_copasst_or_three_signers_is_refused(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = _ready(flow)
    management = (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.ADMINISTRATOR)
    assert detail_of(flow.signatory_policy(zone, management))[2] == (
        "catalog_workers_representation_missing"
    )
    two = flow.as_installer(
        zone,
        "PUT",
        f"/plants/{zone.plant_id}/signatory-policy",
        {"required_roles": ["coordinator_sst", "copasst"], "minimum": 2},
    )
    assert detail_of(two)[2] == "catalog_fewer_than_three"
    policy = ok(flow.as_installer(zone, "GET", f"/plants/{zone.plant_id}/signatory-policy"))
    assert policy["configured"] is False

    ok(flow.signatory_policy(zone))
    signers = flow.signatories(zone)
    without_copasst = [s for s in signers if s.role is not Role.COPASST]
    extra = flow.signatory(zone, Role.ADMINISTRATOR)
    assert (
        detail_of(flow.agreement(zone, [*without_copasst, extra]))[2],
        detail_of(flow.agreement(zone, without_copasst))[2],
    ) == (
        # La administración no es un rol de la política de esta planta.
        "catalog_signatory_role_not_in_policy",
        # Dos firmantes sin ``copasst``: falta primero la representación de los trabajadores.
        "catalog_workers_representation_missing",
    )
    # Un usuario que no tiene el rol con el que se le nombra.
    impostor = Signatory(extra.user_id, Role.COPASST, extra.cookie)
    assert detail_of(flow.agreement(zone, [*without_copasst, impostor]))[2] == (
        "catalog_signatory_user_role_mismatch"
    )
    assert (
        gob.fetch(
            "SELECT count(*) AS n FROM catalog.use_agreement WHERE zone_id = $1", zone.zone_id
        )[0]["n"]
        == 0
    )


def test_g14_only_the_expected_signers_confirm_each_in_their_own_session(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = _ready(flow)
    ok(flow.signatory_policy(zone))
    signers = flow.signatories(zone)
    agreement = ok(flow.agreement(zone, signers), 201)
    agreement_id = agreement["agreement_id"]

    # Ni otra persona con un rol firmante, ni la administración, ni el instalador.
    outsiders = [
        flow.signatory(zone, Role.COORDINATOR_SST),
        flow.signatory(zone, Role.ADMINISTRATOR),
        flow.signatory(zone, Role.LINE_MANAGER),
    ]
    for outsider in outsiders:
        refused = flow.confirm(agreement_id, outsider)
        assert detail_of(refused) == (409, "conflict", "catalog_signatory_not_expected"), (
            outsider.role,
            refused.text,
        )
    assert _confirmations(gob, agreement_id) == []

    # Dos de tres no bastan.
    by_role = {s.role: s for s in signers}
    for role in (Role.COORDINATOR_SST, Role.PLANT_MANAGER):
        assert flow.confirm(agreement_id, by_role[role]).status_code == 201
    assert detail_of(flow.approve(zone, agreement_id))[2] == "catalog_signatures_incomplete"
    assert flow.mode(zone) == "commissioning"

    # El copasst confirma desde la transparencia, sin permisos de gestión.
    assert flow.confirm(agreement_id, by_role[Role.COPASST]).status_code == 201
    rows = _confirmations(gob, agreement_id)
    assert [(row["user_id"], row["role_in_use"]) for row in rows] == [
        (s.user_id, s.role.value) for s in (by_role[r] for r in SIGN_ORDER)
    ]
    origins = {row["role_in_use"]: row["origin"] for row in rows}
    assert origins["copasst"] == "transparency"
    ok(flow.approve(zone, agreement_id))
    assert flow.mode(zone) == "productive"


SIGN_ORDER = (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST)
