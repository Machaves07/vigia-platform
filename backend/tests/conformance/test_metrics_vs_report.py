"""Métricas de la plataforma frente al informe del kit (NFR-GOB-54, NFR-CTR-42; TASK-230).

La suite del kit de U-01 corre contra ``vigia-api`` en el proceso de la prueba (raíz de
producción, ``MeterProvider`` con exportador en memoria) detrás de su balanceador local. El kit
corre en un **proceso aparte** (``tests.conformance.kit_counting``): ``run_checks`` con un
``PlatformUrl`` cuyo transporte anota **lo que el kit recibió** de cada petición a
``/api/nodes`` (la operación del esqueleto y el resultado leído con el ``Reply`` del kit). Esos
conteos son las métricas del ``SuiteReport`` (``metrics``, como las de la plataforma simulada) y
se imprimen en su informe; ``ConformanceReport`` (el JSON del contrato) no lleva conteos para un
objetivo por URL. En el mismo proceso, el kit competiría con la API por el GIL y duplicaría la
duración del check «backend (pruebas de integración)».

Se comparan con ``node_requests_total`` de la plataforma (incremento durante la ejecución) por
operación y resultado: aceptados por tipo de registro, ``accepted_duplicate`` y rechazados **por
``rejection_code``**. Cualquier diferencia falla y la nombra. Con el perfil ``ci`` el kit corre
los grupos que producen esos resultados (``METRIC_GROUPS``); con ``nightly``, la suite entera.
La comparación y el informe quedan en el directorio de informes (artefacto
``informe-conformidad`` de ``nightly.yml``).

Las pruebas de ``compare`` (sin contenedores) alteran a propósito un contador y comprueban que la
comparación falla nombrándolo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from vigia_contracts.conformance.checks import GROUPS
from vigia_contracts.models.receipt import Receipt
from vigia_contracts.server_skeleton import OPERATIONS

from tests.conformance.conftest import (
    PlatformTarget,
    conformance_profile,
    conformance_seed,
)
from tests.conformance.kit_counting import MOUNTED, UNMATCHED, kit_outcome, operation_of
from tests.conformance.platform_target import BACKEND
from vigia_platform.shared.api.declarations import NODE_PREFIX

METRIC: Final = "node_requests_total"
METRIC_GROUPS: Final[Mapping[str, tuple[str, ...]]] = {
    "ci": ("schema", "idempotency", "rejection", "gates"),
    "nightly": (),
}
"""Grupos del kit por perfil (vacío: todos). En ``ci``, los que producen aceptados por tipo,
``accepted_duplicate`` y rechazos por ``rejection_code``; la suite entera ya corre por la orden
``vigia-conformance`` en ``test_conformance_suite.py``."""
TIMEOUT_SECONDS: Final = {"ci": 1_800.0, "nightly": 12_600.0}
"""Tope del proceso del kit (nunca decide el resultado): 30 min en ``ci`` y 3,5 h en ``nightly``."""

type Outcomes = Mapping[tuple[str, str], int]
"""``(operation_id, resultado)`` → peticiones; resultado: ``accepted``,
``accepted_duplicate``, ``not_found`` o el ``rejection_code``."""

_BY_PATH: Final = {NODE_PREFIX + op.path: op_id for op_id, op in MOUNTED.items()}
RECEIPT_OPERATIONS: Final = frozenset(
    operation_id
    for operation_id, operation in OPERATIONS.items()
    if any(
        response.status_code == 200 and response.model is Receipt
        for response in operation.responses
    )
)
"""Las operaciones cuya respuesta correcta es un ``Receipt``: solo en ellas ve el kit si una
aceptación es ``accepted_duplicate`` (en el latido, la concesión o el catálogo, un duplicado
recibe la misma respuesta que el original). En las demás se comparan las aceptaciones juntas."""


def comparable(outcomes: Outcomes) -> Counter[tuple[str, str]]:
    """``outcomes`` con ``accepted_duplicate`` como ``accepted`` fuera de ``RECEIPT_OPERATIONS``."""
    folded: Counter[tuple[str, str]] = Counter()
    for (operation_id, result), count in outcomes.items():
        if result == "accepted_duplicate" and operation_id not in RECEIPT_OPERATIONS:
            result = "accepted"
        folded[(operation_id, result)] += count
    return folded


# --- Lado de la plataforma ----------------------------------------------------------------------


def platform_outcomes(reader: InMemoryMetricReader) -> Counter[tuple[str, str]]:
    """``node_requests_total`` acumulado por (operación, resultado o ``rejection_code``)."""
    found: Counter[tuple[str, str]] = Counter()
    data = reader.get_metrics_data()
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != METRIC:
                    continue
                for point in metric.data.data_points:
                    if not isinstance(point, NumberDataPoint):
                        continue
                    attributes = dict(point.attributes or {})
                    operation_id = _BY_PATH.get(str(attributes.get("route")), UNMATCHED)
                    result = str(attributes.get("result"))
                    if result == "rejected":
                        result = str(attributes.get("rejection_code"))
                    found[(operation_id, result)] += int(point.value)
    return found


# --- Lado del kit -------------------------------------------------------------------------------


def run_counting_kit(
    target: PlatformTarget, profile: str, seed: int, out: Path
) -> subprocess.CompletedProcess[str]:
    """``tests.conformance.kit_counting`` en un proceso aparte, sin credenciales en su entorno."""
    command = [
        sys.executable,
        "-m",
        "tests.conformance.kit_counting",
        "--nodes-url",
        target.nodes.url,
        "--app-url",
        target.app.url,
        "--provision",
        str(target.provisioned.provision_file),
        "--profile",
        profile,
        "--seed",
        str(seed),
        "--out",
        str(out),
    ]
    for group in METRIC_GROUPS[profile]:
        command += ["--group", group]
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AWS_", "VIGIA_", "PG"))
    }
    return subprocess.run(
        command,
        cwd=BACKEND,
        env=environ,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS[profile],
        check=False,
    )


# --- Comparación ----------------------------------------------------------------------------------


def compare(platform: Outcomes, kit: Outcomes) -> list[str]:
    """Cada (operación, resultado) en que difieren los conteos, nombrado; vacío si coinciden."""
    return [
        f"{operation} {result}: plataforma {platform.get((operation, result), 0)},"
        f" informe del kit {kit.get((operation, result), 0)}"
        for operation, result in sorted(set(platform) | set(kit))
        if platform.get((operation, result), 0) != kit.get((operation, result), 0)
    ]


def _document(outcomes: Outcomes) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for (operation, result), count in sorted(outcomes.items()):
        grouped.setdefault(operation, {})[result] = count
    return grouped


def test_compare_names_every_altered_counter() -> None:
    kit = {("post_finding", "accepted"): 3, ("post_finding", "schema_invalid"): 2}
    assert compare(dict(kit), kit) == []
    altered = {**kit, ("post_finding", "schema_invalid"): 1}
    assert compare(altered, kit) == ["post_finding schema_invalid: plataforma 1, informe del kit 2"]
    missing = {("post_finding", "accepted"): 3}
    assert compare(missing, kit) == ["post_finding schema_invalid: plataforma 0, informe del kit 2"]
    extra = {**kit, ("post_heartbeat", "rate_limited"): 1}
    assert compare(extra, kit) == ["post_heartbeat rate_limited: plataforma 1, informe del kit 0"]


def test_duplicates_are_only_told_apart_where_the_kit_sees_a_receipt() -> None:
    assert {
        "post_finding",
        "post_detection_review",
        "post_observability_event",
        "post_update_result",
    } == RECEIPT_OPERATIONS
    outcomes = {
        ("post_heartbeat", "accepted"): 2,
        ("post_heartbeat", "accepted_duplicate"): 1,
        ("post_finding", "accepted_duplicate"): 1,
    }
    assert comparable(outcomes) == {
        ("post_heartbeat", "accepted"): 3,
        ("post_finding", "accepted_duplicate"): 1,
    }


def test_operations_and_outcomes_are_read_like_the_kit() -> None:
    assert operation_of("POST", "/api/nodes/findings") == "post_finding"
    assert operation_of("GET", "/api/nodes/zones/0192f0c4-0000-7000-8000-000000000001/catalog") == (
        "get_zone_catalog"
    )
    assert operation_of("POST", "/api/nodes/clip-uploads/x/confirmation") == (
        "post_clip_upload_confirmation"
    )
    assert operation_of("GET", "/api/nodes/findings") == UNMATCHED
    assert operation_of("GET", "/api/nodes/conformance-profile") == UNMATCHED  # A-51
    assert kit_outcome("post_heartbeat", 404, b"") == "not_found"
    rejection = json.dumps(
        {
            "code": "node_zone_mismatch",
            "retryable": False,
            "message_es": "La zona no es del nodo.",
            "contract_version": "1.0.0",
        }
    ).encode()
    assert kit_outcome("post_finding", 403, rejection) == "node_zone_mismatch"


def test_the_ci_groups_are_kit_groups() -> None:
    assert set(METRIC_GROUPS) == set(TIMEOUT_SECONDS) == {"ci", "nightly"}
    assert set(METRIC_GROUPS["ci"]) <= set(GROUPS)
    assert METRIC_GROUPS["nightly"] == ()


@pytest.mark.integration
def test_platform_metrics_match_the_kit_report(
    platform_target: PlatformTarget, report_directory: Path, tmp_path: Path
) -> None:
    profile, seed = conformance_profile(), conformance_seed()
    reader = platform_target.api.reader
    name = f"metricas-{profile}-{seed}"
    before = platform_outcomes(reader)
    completed = run_counting_kit(platform_target, profile, seed, tmp_path / name)
    after = platform_outcomes(reader)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    document = json.loads((tmp_path / f"{name}-kit.json").read_text(encoding="utf-8"))
    report_text = (tmp_path / f"{name}-informe.txt").read_text(encoding="utf-8")
    for suffix in ("-informe.txt", "-informe.json"):
        (report_directory / f"{name}{suffix}").write_bytes(
            (tmp_path / f"{name}{suffix}").read_bytes()
        )
    platform = comparable(after - before)
    kit = comparable({(op, result): count for op, result, count in document["metrics"] or []})
    differences = compare(platform, kit)
    comparison = {
        "profile": profile,
        "seed": seed,
        "groups": list(METRIC_GROUPS[profile]) or "todos",
        "passed": document["passed"],
        "platform": _document(platform),
        "kit_report": _document(kit),
        "differences": differences,
    }
    (report_directory / f"{name}.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(report_text)

    assert sum(kit.values()) > 0
    assert document["metrics_are_the_transport_counts"] is True
    assert {result for _, result in kit} >= {"accepted", "accepted_duplicate"}
    assert differences == [], "\n".join(differences)
    assert document["passed"] is True, report_text
