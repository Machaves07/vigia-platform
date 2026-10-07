"""NFR-GOB-09: verificación de evidencias sin descargar (TASK-233; PAT-GOB-REN-05 y REN-08).

Banco contra el almacén compatible con S3 local (LocalStack), con el ``EvidenceVerifier`` que usa
el escritor del expediente en la ingesta: ``HeadObject`` con ``ChecksumMode=ENABLED`` (existencia,
tamaño, ``sha256`` y marca de anonimización), **en paralelo** para las referencias de un mismo
hallazgo. Los objetos son clips de verdad: concesión por ``POST clip-uploads`` y ``PUT`` a
``vigia-evidence`` con las cabeceras exactas de la concesión, en una zona de 8 cámaras.

- ``gob_head_object_one``: un objeto; objetivo p95 ≤ 50 ms `[objetivo propio]`;
- ``gob_head_object_16_parallel``: el caso máximo del contrato (8 cámaras x 2 clips = 16
  objetos) en paralelo; objetivo p95 ≤ 200 ms `[objetivo propio]`.

Factor de regresión 1,2 (NFR-GOB-64). Perfil ``nightly``. Solo datos generados.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Final

import pytest
from vigia_contracts.models.clip_reference import ClipReference

from tests.benchmarks.conftest import GOB_REGRESSION_FACTOR, Measure
from tests.benchmarks.gob_support import GRANT_SPACING_SECONDS, MAX_CAMERAS, catalog_zone
from tests.gob_platform_support import GobPlatform
from vigia_platform.ledger.evidence import EvidenceOwner, EvidenceVerifier

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

CLIPS_PER_CAMERA: Final = 2
ONE_MS: Final = 50.0
SIXTEEN_MS: Final = 200.0


def _uploaded(gob: GobPlatform) -> tuple[EvidenceOwner, list[ClipReference]]:
    flow, zone = catalog_zone(gob, cameras=MAX_CAMERAS, productive=True)
    refs: list[ClipReference] = []
    for camera in range(MAX_CAMERAS):
        for _ in range(CLIPS_PER_CAMERA):
            gob.advance(GRANT_SPACING_SECONDS)
            started = gob.now() - timedelta(minutes=5)
            clip = flow.clip(zone, started, started + timedelta(seconds=30), camera=camera)
            refs.append(ClipReference.model_validate_json(json.dumps(clip)))
    owner = EvidenceOwner(zone.organization_id, zone.plant_id, zone.zone_id, zone.node)
    return owner, refs


CASES: Final = {
    "one": ("HeadObject de un clip con suma y marca de anonimización", 1, ONE_MS),
    "16_parallel": (
        "HeadObject en paralelo de los 16 clips de un hallazgo (8 cámaras x 2)",
        MAX_CAMERAS * CLIPS_PER_CAMERA,
        SIXTEEN_MS,
    ),
}
"""Caso → (etiqueta, objetos por verificación, objetivo p95 en ms)."""


@pytest.mark.parametrize("case", list(CASES))
def test_nfr_gob_09_head_object(gob: GobPlatform, measure: Measure, case: str) -> None:
    label, objects, objective = CASES[case]
    owner, refs = _uploaded(gob)
    verifier = EvidenceVerifier(gob.storage, gob.clock)
    assert len(refs) == MAX_CAMERAS * CLIPS_PER_CAMERA
    checks = gob.run(verifier.verify_references(owner, refs))
    assert [check.failure for check in checks] == [None] * len(refs)
    outcomes: list[Any] = []
    turn = [0]

    def target() -> None:
        # Cada ronda rota el primer objeto: el caso de uno no repite siempre la misma clave.
        turn[0] += 1
        start = turn[0] % len(refs)
        chosen = (refs[start:] + refs[:start])[:objects]
        outcomes.extend(gob.run(verifier.verify_references(owner, chosen)))

    measure(
        f"gob_head_object_{case}",
        f"{label} (NFR-GOB-09)",
        target,
        objective_ms=objective,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"objects": objects, "store": "LocalStack S3", "checksum_mode": "ENABLED"},
    )
    assert outcomes and all(check.failure is None for check in outcomes)
