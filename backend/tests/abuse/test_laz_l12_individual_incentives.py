"""L-12: MAN-05/REN-01, sin desagregación individual y con aviso fijo de uso.

Mutación: retirar ``Depends(exact_query("zone_id", "period"))`` de exposure_router.
"""

import pytest

from tests.exposure_support import exposure_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.accreditation.exposure.indicator import USAGE_NOTICE_ES

pytestmark = pytest.mark.integration


def test_laz_l12_no_person_team_or_shift_bonus_query(postgres_endpoint: PostgresEndpoint) -> None:
    with exposure_stack(postgres_endpoint, "laz_l12_incentives") as stack:
        organization = stack.world.organization()
        who = stack.member(organization)
        params = {"zone_id": str(organization.zone_id), "period": stack.period()}
        for field in ("person_id", "team_id", "shift", "group_by"):
            response = stack.get(who, {**params, field: "bonus"})
            assert response.status_code == 400, response.json()
            assert response.json()["code"] == "invalid_request"
        response = stack.get(who, params)
        assert response.status_code == 200, response.text
        assert response.json()["usage_notice_es"] == USAGE_NOTICE_ES
