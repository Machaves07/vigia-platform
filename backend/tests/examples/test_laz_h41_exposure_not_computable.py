"""H-41 · el indicador de exposición no calculable (TASK-322; RF-PLA-19; BR-LAZ-115 y 120;
PR-LAZ-37; SUP-U05-31).

Historia: la gerente de planta consulta el indicador de exposición del mes de una zona cuyas
señales declaradas son todas auxiliares: ninguna señal dura. Sin el estado de la máquina no se
distingue la presencia de la exposición, así que el indicador **se declara no calculable en lugar
de mostrar un valor parcial** (H-41):

- ``GET /accreditation/exposure`` responde ``400 invalid_request`` con ``detail_code``
  ``accreditation_exposure_not_computable`` (A-68) y ninguna cifra: ni minutos ni un cero;
- ``ExposurePort`` (lo que toma el paquete mensual) devuelve ``not_computable`` con su motivo y
  **sin cifras** (``None``, nunca cero; BR-LAZ-115), y **no se guarda ninguna fila**;
- la zona tiene un hallazgo confirmado de ``coexistence`` ``tier_1`` en el mes: no es la falta de
  datos lo que lo bloquea, es la señal. Cuando el catálogo vuelve a declarar la señal de energía,
  la misma consulta da ``200`` con sus tres cifras;
- una zona sin catálogo vigente tampoco es calculable.

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``exposure_stack``). Solo datos
generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest

from tests.exposure_support import ExposureStack, exposure_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.accreditation.exposure.indicator import (
    NO_CATALOG_REASON_ES,
    NO_HARD_SIGNAL_REASON_ES,
)
from vigia_platform.loop.domain.enums import ExposureStatus
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

_NOT_COMPUTABLE = "accreditation_exposure_not_computable"
_FIGURES = ("confirmed_minutes", "pending_classification_minutes", "observable_coverage_percent")


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExposureStack]:
    with exposure_stack(postgres_endpoint, "laz_h41_exposure") as built:
        yield built


def test_h41_a_zone_without_hard_signal_is_not_computable_and_never_zero(
    stack: ExposureStack,
) -> None:
    organization = stack.world.organization()
    manager = stack.member(
        organization, Role.PLANT_MANAGER, ScopeLevel.PLANT, organization.plant_id
    )
    now = stack.now()
    stack.finding(organization, started=now - timedelta(minutes=20), duration=timedelta(seconds=30))
    stack.set_signal_roles(organization, "auxiliary")
    period = stack.period()
    params = {"zone_id": str(organization.zone_id), "period": period}

    response = stack.get(manager, params)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["detail_code"] == _NOT_COMPUTABLE
    assert not set(_FIGURES) & set(body)
    assert response.headers["Cache-Control"] == "no-store"
    assert stack.rows(organization.zone_id) == []

    indicator = stack.run(
        stack.service().indicator(stack.context(organization), organization.zone_id, period)
    )
    assert indicator.status is ExposureStatus.NOT_COMPUTABLE
    assert indicator.not_computable_reason_es == NO_HARD_SIGNAL_REASON_ES
    assert (
        indicator.confirmed_minutes,
        indicator.pending_classification_minutes,
        indicator.observable_coverage_percent,
        indicator.exposure_id,
        indicator.coverage_timeline,
    ) == (None, None, None, None, None)
    assert indicator.hourly_distribution == ()
    assert stack.rows(organization.zone_id) == []

    # La señal de energía vuelve al catálogo: el mismo mes es calculable, con sus tres cifras.
    stack.set_signal_roles(organization, "energy")
    response = stack.get(manager, params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confirmed_minutes"] == 0.5
    assert body["pending_classification_minutes"] == 0.0
    assert body["status"] in {"computed", "partial"}
    assert len(stack.rows(organization.zone_id)) == 1


def test_h41_a_zone_without_current_catalog_is_not_computable(stack: ExposureStack) -> None:
    organization = stack.world.organization()
    coordinator = stack.member(organization)
    zone_id = stack.zone_without_catalog(organization)
    params = {"zone_id": str(zone_id), "period": stack.period()}

    response = stack.get(coordinator, params)

    assert response.status_code == 400, response.text
    assert response.json()["detail_code"] == _NOT_COMPUTABLE
    indicator = stack.run(
        stack.service().indicator(stack.context(organization), zone_id, stack.period())
    )
    assert indicator.status is ExposureStatus.NOT_COMPUTABLE
    assert indicator.not_computable_reason_es == NO_CATALOG_REASON_ES
    assert indicator.confirmed_minutes is None
    assert stack.rows(zone_id) == []
