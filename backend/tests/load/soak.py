"""Perfil ``soak``: 8 horas a ritmo real contra ``staging-<n>`` (LC-GOB-22; NFR-GOB-06; TASK-231).

``uv run python tests/load/soak.py --target https://staging-<n>.<dominio> --fleet fleet.json
--console-session sesion.json``: los nodos simulados de U-01 (``tests.load.driver``) a
``speed_factor`` 1 con 77 episodios por día y zona contra el balanceador de nodos del entorno
(``https://staging-<n>-nodes.<dominio>``, ``infra/stacks/edge.py``; ``--nodes-url`` lo cambia),
con las credenciales que la autoridad de ese entorno emitió a su flota (``--fleet``, el formato
de ``tests.load.provision``), y el cliente sintético de consola contra ``--target`` con una sesión
real (``--console-session``: ``{"cookie": …, "concession_id": …}``). Aquí los **objetivos
absolutos** de NFR-GOB-01 a 05 son umbral **bloqueante** (infrastructure-design §9.2): el código
de salida es 1 si uno se incumple, si se pierde o duplica un registro o si algo queda en cola
muerta. Lo ejecuta TASK-238 (VIG-174) desde ``release.yml``.

``--dry-run`` valida los argumentos y el aprovisionamiento **sin tocar la red**: la forma de las
URL (sin resolverlas), la flota si se da (cada credencial se lee de su archivo, es del nodo
declarado y sigue vigente al terminar la ejecución; los catálogos y la configuración inicial
cumplen el contrato), el conjunto sellado sintético y el plan de cada nodo con la semilla. Imprime
el informe JSON (objetivo, nodos, semilla, plan y umbrales absolutos) y lo deja en ``--report`` o
``VIGIA_LOAD_REPORT_DIR`` si se indica. Sin ``--fleet``, el aprovisionamiento queda ``pending``.

Ni el informe ni la salida llevan un código de alta, un PEM ni una URL prefirmada (PR-GOB-31).
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # ``python tests/load/soak.py``: el paquete ``tests`` es de backend/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import re
import secrets
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final
from urllib.parse import urlsplit

from vigia_contracts.conformance.simulated_node.dataset import load_sealed_dataset
from vigia_contracts.conformance.simulated_node.schedule import DAY_MS
from vigia_contracts.credentials import FileCredentialStore
from vigia_contracts.models.tolerant.node_enrollment import (
    NodeInitialConfiguration,
)

from tests.load.profiles import (
    ABSOLUTE_TARGETS,
    PROFILES,
    LoadProfile,
    write_sealed_dataset,
)
from tests.load.report import REPORT_DIR_VARIABLE, secret_findings
from vigia_platform.shared.clock import SystemClock

__all__ = ["SoakSettings", "evaluate_thresholds", "main"]

SEED_VARIABLE: Final = "VIGIA_LOAD_SEED"
MAX_SEED: Final = 2**53 - 1
FULL_HOURS: Final = 8.0
TARGET_PATTERN: Final = re.compile(
    r"^staging-(?P<n>[0-9]{1,7})\.(?P<domain>[a-z0-9.-]+\.[a-z]{2,})$"
)
"""``staging-<n>.<dominio>`` (``infra/stacks/edge.py``)."""
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
WALL: Final = SystemClock()
FLUSH_SECONDS: Final = 1_800.0


class SoakError(ValueError):
    """Argumento o aprovisionamiento inválido (mensaje en español, sin secretos)."""


@dataclass(frozen=True)
class SoakSettings:
    target: str
    nodes_url: str
    seed: int
    hours: float
    fleet: Path | None
    console_session: Path | None
    report_dir: Path | None
    dry_run: bool

    @property
    def profile(self) -> LoadProfile:
        return replace(PROFILES["soak"], steady_minutes=self.hours * 60)


def _https_origin(url: str, option: str) -> tuple[str, str]:
    """``(origen, anfitrión)`` de una URL ``https://anfitrión[:puerto]`` sin ruta, consulta ni
    credenciales; nunca la resuelve."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SoakError(f"{option} debe ser https://<anfitrión>")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SoakError(f"{option} no admite credenciales, consulta ni fragmento")
    if parts.path not in ("", "/"):
        raise SoakError(f"{option} es el origen, sin ruta")
    try:
        port = parts.port
    except ValueError:
        raise SoakError(f"{option} tiene un puerto inválido") from None
    host = parts.hostname.lower()
    return f"https://{host}" + (f":{port}" if port else ""), host


def settings_from(arguments: argparse.Namespace) -> SoakSettings:
    target, host = _https_origin(arguments.target, "--target")
    match = TARGET_PATTERN.match(host)
    if match is None:
        raise SoakError("--target es https://staging-<n>.<dominio> (el entorno efímero)")
    nodes_url = arguments.nodes_url or f"https://staging-{match['n']}-nodes.{match['domain']}"
    nodes_url, _ = _https_origin(nodes_url, "--nodes-url")
    if not 0 < arguments.hours <= FULL_HOURS:
        raise SoakError(f"--hours va de más de 0 a {FULL_HOURS:g}")
    if arguments.seed is not None and not 0 <= arguments.seed <= MAX_SEED:
        raise SoakError("--seed va de 0 a 2^53 - 1")
    seed = arguments.seed
    if seed is None:
        requested = os.environ.get(SEED_VARIABLE)
        seed = int(requested) if requested else secrets.randbelow(MAX_SEED + 1)
    if not arguments.dry_run and (arguments.fleet is None or arguments.console_session is None):
        raise SoakError("sin --dry-run hacen falta --fleet y --console-session")
    report_dir = arguments.report or (
        Path(os.environ[REPORT_DIR_VARIABLE]) if os.environ.get(REPORT_DIR_VARIABLE) else None
    )
    return SoakSettings(
        target=target,
        nodes_url=nodes_url,
        seed=seed,
        hours=arguments.hours,
        fleet=arguments.fleet,
        console_session=arguments.console_session,
        report_dir=report_dir,
        dry_run=arguments.dry_run,
    )


# --- Aprovisionamiento (sin red) ------------------------------------------------------------------


def check_fleet(settings: SoakSettings, ends_at: dt.datetime) -> dict[str, Any]:
    """Valida ``fleet.json`` leyendo solo archivos: base de ingesta del entorno, raíz del servidor,
    y por nodo su credencial (del nodo declarado y vigente hasta ``ends_at``), su configuración
    inicial y sus catálogos."""
    if settings.fleet is None:
        return {"status": "pending", "reason": "sin --fleet: la flota del entorno la da TASK-238"}
    try:
        fleet = json.loads(settings.fleet.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SoakError(f"--fleet no se pudo leer ({type(error).__name__})") from None
    expected = settings.nodes_url + "/api/nodes"
    if fleet.get("ingest_base_url") != expected:
        raise SoakError(f"la flota no apunta a {expected}")
    if not Path(str(fleet.get("verify", ""))).is_file():
        raise SoakError("la raíz del servidor del entorno (verify) no existe")
    nodes = fleet.get("nodes") or []
    if not nodes:
        raise SoakError("la flota no tiene nodos")
    zones = 0
    earliest: dt.datetime | None = None
    for position, entry in enumerate(nodes):
        where = f"nodes[{position}]"
        credential = FileCredentialStore(entry["credential"]).load()
        if credential is None:
            raise SoakError(f"{where}: falta la credencial")
        if credential.identity.node_id != str(entry["node_id"]).lower():
            raise SoakError(f"{where}: la credencial es de otro nodo")
        expires = credential.current.expires_at
        earliest = expires if earliest is None else min(earliest, expires)
        if expires <= ends_at:
            raise SoakError(f"{where}: la credencial vence antes de terminar el soak")
        NodeInitialConfiguration.model_validate_json(
            Path(entry["configuration"]).read_text(encoding="utf-8")
        )
        catalogs = entry.get("catalogs") or []
        if not catalogs:
            raise SoakError(f"{where}: sin catálogos")
        zones += len(catalogs)
    return {
        "status": "valid",
        "nodes": len(nodes),
        "zones": zones,
        "earliest_credential_expiry": earliest.isoformat() if earliest else None,
    }


def plan_summary(settings: SoakSettings, dataset: Path, nodes: int, zones: int) -> dict[str, Any]:
    """El conjunto sellado sintético verificado por el kit y el plan de cada nodo con la semilla
    (sin red): cuántos registros emitirá la flota en la duración del perfil."""
    from tests.load.driver import plan_items

    names = load_sealed_dataset(dataset).names
    per_node = max(1, round(zones / nodes))
    planned = sum(
        len(plan_items(settings.profile, settings.seed, names, index, per_node))
        for index in range(nodes)
    )
    return {
        "images": len(names),
        "nodes": nodes,
        "zones": zones,
        "planned_records": planned,
        "simulated_days": math.ceil(settings.profile.span_ms / DAY_MS),
    }


# --- Umbrales absolutos ---------------------------------------------------------------------------


def evaluate_thresholds(
    node_latency: Mapping[str, Mapping[str, Any]], console: Mapping[str, Any]
) -> list[str]:
    """Incumplimientos de los objetivos absolutos (``ABSOLUTE_TARGETS``), cada uno con su ruta:
    NFR-GOB-01 por ruta del contrato medida y NFR-GOB-03 por el veredicto de la consola."""
    failures = []
    for route, limits in ABSOLUTE_TARGETS["NFR-GOB-01"].items():
        measured = node_latency.get(route)
        if not measured or not measured.get("count"):
            continue
        for key in ("p95_ms", "p99_ms"):
            limit = limits.get(key)
            value = measured.get(key)
            if limit is not None and value is not None and value > limit:
                failures.append(f"NFR-GOB-01 {route}: {key} {value} > {limit}")
    failures += [f"NFR-GOB-03 {failure}" for failure in console.get("failures", [])]
    return failures


def thresholds_document() -> dict[str, Any]:
    return {
        "blocking": True,
        "targets": ABSOLUTE_TARGETS,
        "not_measured_here": {
            "NFR-GOB-01 POST /api/nodes/enrollment": "la flota ya está dada de alta",
            "NFR-GOB-02": "a ritmo real no hay vaciado de cola; lo mide nightly como tendencia",
            "NFR-GOB-04": "puertos en proceso: banco de VIG-170",
            "NFR-GOB-05": "la ruta del documento del acta no está publicada en openapi/app.yaml",
        },
    }


# --- Ejecución ------------------------------------------------------------------------------------


def _run(settings: SoakSettings, work: Path, dataset: Path) -> tuple[dict[str, Any], list[str]]:
    """La ejecución real: nodos en este proceso y la consola en un hilo."""
    from tests.load.console_client import ConsoleClient, ConsoleTargets, evaluate
    from tests.load.driver import drive
    from tests.load.provision import ConsoleSession
    from tests.load.report import analyse

    assert settings.fleet is not None and settings.console_session is not None
    fleet = json.loads(settings.fleet.read_text(encoding="utf-8"))
    session = json.loads(settings.console_session.read_text(encoding="utf-8"))
    nodes = fleet["nodes"]
    console = ConsoleClient(
        settings.target,
        Path(fleet["verify"]),
        ConsoleSession(str(session["cookie"]), uuid.UUID(str(session["concession_id"]))),
        ConsoleTargets(
            tuple(uuid.UUID(node["node_id"]) for node in nodes),
            tuple(uuid.UUID(c["zone_id"]) for node in nodes for c in node["catalogs"]),
            tuple({uuid.UUID(node["plant_id"]) for node in nodes}),
        ),
        settings.seed,
    )
    outboxes = work / "bandejas"
    outboxes.mkdir()
    plan = {
        "profile": "soak",
        "seed": settings.seed,
        "fleet": str(settings.fleet),
        "dataset": str(dataset),
        "cache": str(work / "clips"),
        "work": str(outboxes),
        "flush_seconds": FLUSH_SECONDS,
        "steady_minutes": settings.hours * 60,
    }
    console.start()
    try:
        result = asyncio.run(drive(plan))
    finally:
        console.stop()
    analysis = analyse(result)
    verdict = evaluate(console.samples, unpublished=console.unpublished).to_json()
    failures = evaluate_thresholds(analysis["node_latency"], verdict)
    if analysis["accepted"] != analysis["emitted"] or analysis["dead_letter"]:
        failures.append("registros perdidos o en cola muerta")
    return {"results": analysis, "console": verdict}, failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tests/load/soak.py",
        description="Perfil soak de 8 horas a ritmo real contra staging-<n> (umbrales absolutos).",
    )
    parser.add_argument("--target", required=True, help="https://staging-<n>.<dominio>")
    parser.add_argument("--nodes-url", help="balanceador de nodos (por defecto staging-<n>-nodes)")
    parser.add_argument("--fleet", type=Path, help="flota del entorno (fleet.json)")
    parser.add_argument("--console-session", type=Path, help="sesión de consola (JSON)")
    parser.add_argument("--seed", type=int, help="semilla (por defecto VIGIA_LOAD_SEED o al azar)")
    parser.add_argument("--hours", type=float, default=FULL_HOURS, help="duración (8 h)")
    parser.add_argument("--report", type=Path, help="carpeta del informe")
    parser.add_argument("--dry-run", action="store_true", help="valida sin tocar la red")
    arguments = parser.parse_args(argv)
    try:
        settings = settings_from(arguments)
        profile = settings.profile
        ends_at = WALL.now() + dt.timedelta(hours=settings.hours, seconds=FLUSH_SECONDS)
        provisioning = check_fleet(settings, ends_at)
    except SoakError as error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_USAGE
    with tempfile.TemporaryDirectory(prefix="vigia-soak-") as directory:
        work = Path(directory)
        dataset = write_sealed_dataset(work / "conjunto", settings.seed, WALL.now())
        nodes = provisioning.get("nodes", profile.nodes)
        zones = provisioning.get("zones", profile.zones)
        document: dict[str, Any] = {
            "profile": "soak",
            "dry_run": settings.dry_run,
            "target": settings.target,
            "nodes_url": settings.nodes_url,
            "seed": settings.seed,
            "hours": settings.hours,
            "full_duration": settings.hours == FULL_HOURS,
            "speed_factor": profile.speed_factor,
            "episodes_per_day": profile.episodes_per_day,
            "provisioning": provisioning,
            "plan": plan_summary(settings, dataset, nodes, zones),
            "absolute_thresholds": thresholds_document(),
        }
        code = EXIT_OK
        if not settings.dry_run:
            outcome, failures = _run(settings, work, dataset)
            document.update(outcome)
            document["failures"] = failures
            code = EXIT_FAILED if failures else EXIT_OK
    text = json.dumps(document, ensure_ascii=False, indent=2, default=str)
    leaks = secret_findings({"informe": text})
    if leaks:
        sys.stderr.write("error: el informe contiene material sensible: " + ", ".join(leaks) + "\n")
        return EXIT_FAILED
    if settings.report_dir is not None:
        settings.report_dir.mkdir(parents=True, exist_ok=True)
        kind = "dry-run" if settings.dry_run else "run"
        (settings.report_dir / f"soak-{kind}-{settings.seed}.json").write_text(
            text, encoding="utf-8"
        )
    sys.stdout.write(text + "\n")
    sys.stdout.write(
        f"soak {'en seco' if settings.dry_run else 'ejecutado'}: objetivo {settings.target},"
        f" semilla {settings.seed} (reproducir: {SEED_VARIABLE}={settings.seed})\n"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
