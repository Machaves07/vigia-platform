"""G-8 · Reutilización del acuerdo de uso de otra zona (business-rules §12; P9; H-45).

**Qué intenta**: activar una zona nueva citando el acuerdo ya firmado y aprobado de otra zona de
la misma planta, sin pasar por su acta ni por sus firmas.

**Qué lo detiene**:

- BR-GOB-31: un acuerdo pertenece a una zona; citarlo desde otra
  (``replaces_agreement_id``) es ``catalog_agreement_reused_from_other_zone`` y no escribe nada;
  aprobar el acuerdo de la otra zona no toca la compuerta de esta;
- BR-GOB-29: la aprobación exige, en orden, montaje aprobado, acta de comisionamiento cerrada **de
  esa zona**, firmas completas y política de planta: el primer requisito que falta decide el error
  (``catalog_mounting_gate_pending``, ``catalog_commissioning_record_missing``…).

El relevo legítimo, en la **misma** zona (BR-GOB-32), deja el acuerdo aprobado vigente mientras el
sustituto espera firmas: ``GateQueryPort.current_agreement`` sigue devolviendo el aprobado aunque
el pendiente sea más reciente (revisión de VIG-153: sin el filtro ``status = 'approved'``, el
pendiente, con ``approved_at`` nulo, saldría primero).

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import uuid

import pytest

from tests.gob_platform_support import GobPlatform, Onboarding, detail_of, ok
from tests.writer_support import unit_context
from vigia_platform.catalog.adapters.postgres.gate_query import PostgresGateQuery
from vigia_platform.shared.context import ActorKind, ActorUnit

pytestmark = pytest.mark.integration


def test_g08_an_agreement_of_another_zone_is_never_reused(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    first = flow.productive_zone()
    second = flow.zone(within=first)
    agreements_before = gob.fetch(
        "SELECT count(*) AS n FROM catalog.use_agreement WHERE zone_id = $1", second.zone_id
    )[0]["n"]

    # Antes del montaje de la segunda zona: el primer requisito que falta es el montaje.
    reuse = flow.as_installer(
        second,
        "POST",
        f"/zones/{second.zone_id}/use-agreements",
        {
            "signatories": [
                {"role": s.role.value, "user_id": str(s.user_id)} for s in first.signatories
            ],
            "replaces_agreement_id": str(first.agreement_id),
        },
    )
    assert detail_of(reuse)[2] == "catalog_agreement_reused_from_other_zone", reuse.text
    flow.mount(second)
    reuse_mounted = flow.as_installer(
        second,
        "POST",
        f"/zones/{second.zone_id}/use-agreements",
        {
            "signatories": [
                {"role": s.role.value, "user_id": str(s.user_id)} for s in first.signatories
            ],
            "replaces_agreement_id": str(first.agreement_id),
        },
    )
    assert detail_of(reuse_mounted) == detail_of(reuse)
    assert (
        gob.fetch(
            "SELECT count(*) AS n FROM catalog.use_agreement WHERE zone_id = $1", second.zone_id
        )[0]["n"]
        == agreements_before
    )

    # Aprobar otra vez el acuerdo de la primera zona no abre la segunda.
    again = flow.approve(first, first.agreement_id)  # type: ignore[arg-type]
    assert again.status_code == 200, again.text
    assert flow.mode(second) == "commissioning"

    # Un acuerdo propio de la segunda zona, sin acta de esa zona, no se aprueba (BR-GOB-29).
    ok(flow.signatory_policy(second))
    own = ok(flow.agreement(second, first.signatories), 201)
    for signatory in first.signatories:
        assert flow.confirm(own["agreement_id"], signatory).status_code == 201
    refused = flow.approve(second, own["agreement_id"])
    assert detail_of(refused) == (409, "conflict", "catalog_commissioning_record_missing")
    assert flow.mode(second) == "commissioning"
    # Con su acta, sí: el acuerdo de la segunda zona es el suyo.
    flow.closed_record(second)
    approved = ok(flow.approve(second, own["agreement_id"]))
    assert approved["gates"]["usage"]["agreement_id"] == own["agreement_id"]
    assert approved["gates"]["usage"]["agreement_id"] != str(first.agreement_id)


def test_g08_a_pending_replacement_never_hides_the_approved_agreement(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    approved_id = zone.agreement_id
    replacement = ok(
        flow.as_installer(
            zone,
            "POST",
            f"/zones/{zone.zone_id}/use-agreements",
            {
                "signatories": [
                    {"role": s.role.value, "user_id": str(s.user_id)} for s in zone.signatories
                ],
                "replaces_agreement_id": str(approved_id),
            },
        ),
        201,
    )
    assert replacement["status"] == "pending_signatures"
    # El sustituto es más reciente y no tiene ``approved_at``: el puerto sigue en el aprobado.
    port = PostgresGateQuery(gob.services.database)  # type: ignore[arg-type]
    context = unit_context(zone.organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)
    current = gob.run(port.current_agreement(context, zone.zone_id))
    assert current is not None
    assert current.agreement_id == approved_id
    assert current.agreement_id != uuid.UUID(replacement["agreement_id"])
    assert flow.mode(zone) == "productive"
