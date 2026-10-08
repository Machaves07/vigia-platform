"""Perfil de carga ``ci``: 10 nodos simulados, ``speed_factor`` 60, 5 minutos (NFR-GOB-06,
PR-GOB-01 bajo carga; TASK-231).

Funcional e idempotencia contra la plataforma real del arnés de TASK-230: los nodos simulados de
U-01 (cliente real del contrato, en un proceso aparte) emiten el ritmo del piloto por el
balanceador ``nodes.`` con mTLS, y una fracción de las respuestas a registros ya aceptados se
pierde para que el nodo los reenvíe con la misma clave. Al final:

- aceptados = emitidos, nada sin enviar ni en cola muerta (cero rechazos permanentes);
- cada reenvío por respuesta perdida obtuvo ``accepted_duplicate`` y hubo al menos uno;
- el expediente tiene **exactamente** los registros emitidos, ninguno dos veces (``source_key``);
- cada cadena de planta de la organización verifica entera, en orden de recepción (PR-NUC-13 con
  ``verify_ledger_chains``, la verificación de U-02);
- cero ``rate_limited`` por debajo del mínimo de NFR-CTR-02 y cero ``temporarily_unavailable``;
- la semilla se imprime y va en el informe; ni el informe ni las salidas llevan un código de alta,
  un PEM ni una URL prefirmada (PR-GOB-31).

Corre con la marca ``integration`` en el check «backend (pruebas de integración)».
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest

from tests.load.conftest import LoadTarget, load_seed, run_profile
from tests.load.profiles import PROFILES, LoadProfile
from tests.load.provision import Fleet
from tests.load.report import BulkheadSampler, analyse, secret_findings, write_report
from tests.writer_support import verify_ledger_chains

pytestmark = pytest.mark.integration

NODE_RECORD_TYPES: Final = (
    "finding_received",
    "detection_for_review_received",
    "observability_event_received",
)
"""Los tres registros del contrato en el expediente (``fleet.record_types``)."""
DRIVER_MARGIN_SECONDS: Final = 1_800.0
"""Holgura del tope del proceso de los nodos sobre las fases y el vaciado (nunca decide nada)."""


def ledger_check(target: LoadTarget, fleet: Fleet, emitted: set[str]) -> dict[str, Any]:
    """El expediente de la organización frente a lo emitido: claves de origen, duplicados y la
    verificación completa de cada cadena de planta (orden de recepción incluido)."""
    rows = target.stack.fetch(
        "SELECT source_key, record_type FROM ledger.ledger_record"
        " WHERE organization_id = $1 AND record_type = ANY($2::text[])",
        fleet.organization_id,
        list(NODE_RECORD_TYPES),
    )
    keys = Counter(str(row["source_key"]).lower() for row in rows)
    chains = asyncio.run(verify_ledger_chains(target.stack.migrated, fleet.organization_id))
    return {
        "records": sum(keys.values()),
        "by_type": dict(Counter(str(row["record_type"]) for row in rows)),
        "duplicated_source_keys": sum(1 for count in keys.values() if count > 1),
        "missing": len(emitted - set(keys)),
        "unexpected": len(set(keys) - emitted),
        "chains_verified": len(chains),
        "chain_lengths": sorted(chains.values()),
    }


def run_load(
    target: LoadTarget,
    profile: LoadProfile,
    *,
    dataset: Path,
    cache: Path,
    work: Path,
    console: Any = None,
) -> tuple[Fleet, dict[str, Any], Mapping[str, Any], str]:
    """Aprovisiona la flota, lanza los nodos y devuelve la flota, el resultado, el análisis con
    la ocupación de los mamparos y la salida del proceso de los nodos."""
    seed = load_seed()
    print(f"perfil de carga {profile.name} semilla {seed}")
    fleet = target.provision(profile)
    fleet.wait_until_ready()
    sampler = BulkheadSampler(target.api.reader)
    sampler.start()
    if console is not None:
        console.start(fleet)
    try:
        run = run_profile(
            profile,
            fleet,
            seed=seed,
            dataset=dataset,
            cache=cache,
            work=work,
            timeout=profile.wall_seconds + DRIVER_MARGIN_SECONDS,
        )
    finally:
        if console is not None:
            console.stop()
        occupancy = sampler.stop()
    print(run.output)
    assert run.code == 0, run.output
    assert run.result is not None, run.output
    assert run.result["seed"] == seed
    analysis = {**analyse(run.result), "bulkheads": occupancy}
    return fleet, run.result, analysis, run.output


def finish_report(
    target: LoadTarget,
    fleet: Fleet,
    profile: LoadProfile,
    reports: Path,
    document: Mapping[str, Any],
    output: str,
) -> Path:
    """Escribe el informe y comprueba que ni él, ni la salida de los nodos, ni el registro de la
    plataforma contienen un código de alta, un PEM o una URL prefirmada (PR-GOB-31)."""
    path = write_report(reports, profile.name, load_seed(), document)
    print(f"informe: {path}")
    assert fleet.enrollment_codes
    texts = {"informe": path.read_text(encoding="utf-8"), "salida de los nodos": output}
    texts.update(target.logs())
    assert not secret_findings(texts, fleet.enrollment_codes)
    return path


def saturation_transients(analysis: Mapping[str, Any]) -> dict[str, int]:
    """Los transitorios por saturación que vieron los nodos (NFR-GOB-02): cada
    ``temporarily_unavailable`` por operación y cada ``503`` (``unavailable``) del cliente."""
    codes = analysis["rejection_codes"]
    events = analysis["events"]
    return {
        **{key: n for key, n in codes.items() if key.endswith(":temporarily_unavailable")},
        **{key: n for key, n in events.items() if key.endswith(":unavailable")},
    }


def assert_functional(
    analysis: Mapping[str, Any], ledger: Mapping[str, Any], *, saturation_blocks: bool = True
) -> None:
    """Lo común a ``ci`` y ``nightly``: sin pérdida, sin duplicados, cadena íntegra.

    Con ``saturation_blocks`` (``ci``), ningún transitorio por saturación; sin él (el banco de
    ``nightly``, A-65), los transitorios son tendencia: van al informe y no fallan la ejecución,
    porque NFR-GOB-02 solo bloquea en el ``soak`` sobre ``staging``. Lo demás bloquea siempre."""
    assert analysis["emitted"] > 0
    assert analysis["accepted"] == analysis["emitted"], analysis
    assert analysis["lost"] == 0, analysis
    assert analysis["dead_letter"] == {}, analysis["dead_letter"]
    assert analysis["halted"] == {}, analysis["halted"]
    assert analysis["rate_limited_below_minimum"] == 0, analysis["rate_limited_by_operation"]
    assert analysis["rate_limited_by_operation"] == {}, analysis["rate_limited_by_operation"]
    if saturation_blocks:
        assert not saturation_transients(analysis)
    assert ledger["missing"] == 0 and ledger["unexpected"] == 0, ledger
    assert ledger["records"] == analysis["emitted"], ledger
    assert ledger["duplicated_source_keys"] == 0, ledger
    assert ledger["chains_verified"] >= 1


def test_ci_profile_ten_nodes_lose_nothing_duplicate_nothing_and_keep_the_chain(
    load_target: LoadTarget,
    sealed_dataset: Path,
    clip_cache: Path,
    load_reports: Path,
    tmp_path: Path,
) -> None:
    profile = PROFILES["ci"]
    fleet, result, analysis, output = run_load(
        load_target, profile, dataset=sealed_dataset, cache=clip_cache, work=tmp_path
    )
    ledger = ledger_check(load_target, fleet, set(result["journal"]["emitted"]))
    document = {
        "seed": load_seed(),
        "profile": profile.describe(),
        "target": "local: plataforma real del arnés de TASK-230 (vigia-api en proceso)",
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "results": analysis,
        "ledger": ledger,
        "absolute_targets": "tendencia (infrastructure-design §9.2): ver node_latency",
    }
    finish_report(load_target, fleet, profile, load_reports, document, output)
    print(json.dumps({key: analysis[key] for key in ("emitted", "accepted", "receipts")}))

    assert_functional(analysis, ledger)
    # PR-GOB-01 bajo carga: cada respuesta perdida se reenvió y obtuvo `accepted_duplicate`.
    assert analysis["lost_replies"] > 0
    assert analysis["receipts"].get("accepted_duplicate", 0) == analysis["lost_replies"]
    assert analysis["receipts"].get("accepted", 0) + analysis["lost_replies"] == ledger["records"]
