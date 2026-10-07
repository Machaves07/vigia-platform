"""G-9 · Proveedor bajo concesión que intenta leer hallazgos o firmar un acuerdo (§12).

**Qué intenta**: el instalador del proveedor usa su concesión de comisionamiento para leer los
hallazgos del cliente o para completar las firmas de un acuerdo de uso.

**Qué lo detiene**:

- BR-NUC-37: la concesión da **exactamente** la columna ``provider_installer`` de la matriz:
  catálogo, flota, cobertura y comisionamiento sí; hallazgos, evidencias y etiquetas no;
- BR-GOB-27: ``agreements.sign`` no se obtiene por la concesión (la columna la lista para el
  instalador de la propia proveedora, nunca sobre el cliente): confirmar es ``not_found``, deja
  ``authorization_denied`` con la clave en la auditoría del cliente y ninguna confirmación;
- BR-NUC-09: lo que queda fuera responde ``not_found``, **igual** que un recurso inexistente
  (estado y cuerpo, salvo ``correlation_id``), nunca ``forbidden``.

El aislamiento ruta por ruta bajo concesión es de TASK-228
(``tests/isolation/test_route_isolation.py::test_pr_nuc_01_under_concession_the_provider_installer
_column_decides``); aquí, el abuso de punta a punta sobre una zona productiva con hallazgos.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok
from tests.platform_support import comparable
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration


def _denials(gob: GobPlatform, zone: GobZone) -> list[tuple[str, str]]:
    return [
        (row["operation"], row["key"])
        for row in gob.fetch(
            "SELECT operation, convert_from(filters, 'UTF8')::jsonb ->> 'permission_key' AS key"
            " FROM shared.audit_entry WHERE organization_id = $1 AND actor_concession_id = $2"
            " AND operation = 'authorization_denied' ORDER BY chain_sequence",
            zone.organization_id,
            zone.concession,
        )
    ]


def _finding_record(gob: GobPlatform, zone: GobZone) -> uuid.UUID:
    (row,) = gob.records(zone.organization_id, "finding_received")
    record_id: uuid.UUID = row["record_id"]
    return record_id


def test_g09_the_concession_never_reads_findings_evidence_or_labels(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    assert flow.post_finding(zone, flow.finding(zone)).status_code == 200
    record_id = _finding_record(gob, zone)
    _, coordinator = gob.person(zone.organization_id, Role.COORDINATOR_SST)
    # Control: quien sí tiene la clave lee el hallazgo.
    read = ok(gob.call("GET", f"/ledger/records/{record_id}", cookie=coordinator))
    assert read["record_type"] == "finding_received"

    def as_installer(path: str, params: Any = None) -> Any:
        return gob.call(
            "GET", path, cookie=zone.installer, concession=zone.concession, params=params
        )

    known = as_installer(f"/ledger/records/{record_id}")
    missing = as_installer(f"/ledger/records/{uuid.uuid4()}")
    assert known.status_code == 404 and comparable(known) == comparable(missing)
    listing = as_installer("/ledger/records", {"record_type": "finding_received"})
    assert listing.status_code == 404 and listing.json()["code"] == "not_found"
    assert str(record_id) not in listing.text
    assert as_installer("/labels").status_code == 404
    # La columna del instalador sí: catálogo, compuertas, flota y cobertura de la zona.
    for path in (
        f"/zones/{zone.zone_id}/catalog",
        f"/zones/{zone.zone_id}/gates",
        "/fleet/nodes",
    ):
        assert as_installer(path).status_code == 200, path
    body = json.dumps(ok(as_installer("/fleet/nodes")))
    assert str(record_id) not in body


def test_g09_the_concession_never_signs_a_use_agreement(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    pending = ok(
        flow.as_installer(
            zone,
            "POST",
            f"/zones/{zone.zone_id}/use-agreements",
            {
                "signatories": [
                    {"role": s.role.value, "user_id": str(s.user_id)} for s in zone.signatories
                ],
                "replaces_agreement_id": str(zone.agreement_id),
            },
        ),
        201,
    )
    agreement_id = pending["agreement_id"]
    before = _denials(gob, zone)
    known = flow.as_installer(zone, "POST", f"/use-agreements/{agreement_id}/confirmations")
    missing = flow.as_installer(zone, "POST", f"/use-agreements/{uuid.uuid4()}/confirmations")
    assert known.status_code == 404 and comparable(known) == comparable(missing)
    assert _denials(gob, zone)[len(before) :][:1] == [("authorization_denied", "agreements.sign")]
    assert (
        gob.fetch(
            "SELECT count(*) AS n FROM catalog.agreement_confirmation WHERE agreement_id = $1",
            uuid.UUID(agreement_id),
        )[0]["n"]
        == 0
    )
    # Ni aprobándolo: faltan las firmas que la concesión no puede poner.
    refused = flow.approve(zone, agreement_id)
    assert refused.json().get("detail_code") == "catalog_signatures_incomplete", refused.text
