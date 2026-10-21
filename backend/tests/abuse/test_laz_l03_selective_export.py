"""L-3: SEG-02/REN-04, todos los estados y los huecos declarados, nunca paquete selectivo.

Mutación: filtrar los sujetos ``discarded`` en ``contents._fold_findings`` antes de componerlos.
Reutiliza el mundo sintético H-37, no duplica la historia del inspector.
"""

from tests.examples.test_laz_h37_inspector_package import (
    DETECTION,
    EPISODE,
    ZONE_A,
    _world,
)
from tests.export_contents_support import compose_world
from vigia_platform.accreditation.manifest.model import COVERAGE_NOTICE_ES
from vigia_platform.loop.domain.enums import PackageForm


def test_laz_l03_discarded_findings_and_coverage_gaps_cannot_be_hidden() -> None:
    composed = compose_world(_world(PackageForm.WITHOUT_CLIPS))
    findings = composed.json(f"findings/zone-{ZONE_A}.json")["findings"]
    assert {row["subject_id"]: row["lifecycle_state"] for row in findings} == {
        str(EPISODE): "closed",
        str(DETECTION): "discarded",
    }
    assert (composed.manifest["finding_count"], composed.manifest["discarded_count"]) == (2, 1)
    coverage = composed.json(f"coverage/zone-{ZONE_A}.json")
    assert sum(row["duration_ms"] for row in coverage["intervals"]) == 31 * 24 * 3600 * 1000
    assert any(row["state"] != "observable" for row in coverage["intervals"])
    assert composed.manifest["coverage_notice_es"] == COVERAGE_NOTICE_ES
