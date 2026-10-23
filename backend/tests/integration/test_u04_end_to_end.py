"""Hito «U-04 terminada»: bandeja, lazo y exportación sobre los datos del nodo simulado a través de
U-03, verificador sobre un paquete exportado y adaptador simulado del asistente (TASK-335;
``unit-of-work-dependency.md`` §4).

La plataforma es la del arnés de carga de U-03 (``tests.load``: PostgreSQL y LocalStack de
testcontainers, ``bootstrap``, dos ``vigia-api`` con la raíz de composición de producción detrás de
los balanceadores ``app.`` y ``nodes.``), más un ``vigia-worker`` con la **raíz de composición de
producción** (``compose_worker_runtime`` con todas las unidades registradas) en un hilo del proceso
de la prueba. Lo único cambiado en el worker es el proveedor del asistente: el
``FakeAssistantProvider`` guionizado (``valid_with_citations``) en lugar del adaptador sin clave.

Pasos, cada uno por la ruta HTTP real detrás de ``app.`` con la sesión de una coordinadora SST:

1. **Nodos**: el perfil de carga de U-03 (``VIGIA_U04_E2E_PROFILE``: ``u04-e2e`` por omisión;
   ``ci-real-time`` para el hito, 10 nodos a ``speed_factor`` 1 durante 30 minutos) llena la
   plataforma con el cliente real del contrato: aceptados = emitidos, sin duplicados, cadena
   íntegra (las aserciones del perfil ``ci`` de TASK-231).
2. **Bandeja** (SUP-U05-01): el consumidor ``loop_projection`` del worker la construye; tiene **un
   elemento por episodio** de los hallazgos recibidos y uno por detección enviada a revisión, y
   ninguno más.
3. **Cola** (SUP-U05-05 y 06): una detección de la cola se resuelve como confirmada.
4. **Lazo de cierre** (SUP-U05-03, 07, 08 y 11): un hallazgo se clasifica, se le asigna una
   acción, se añade su evidencia y se cierra con firma.
5. **Incerrable** (SUP-U05-12): con la política de la planta cargada por las rutas de U-03
   (``POST /documents``, subida a LocalStack y ``POST /plants/{plant_id}/policy``), otro hallazgo
   se declara sin alternativa segura.
6. **Asistente** (SUP-U05-16, 19 y 20): sobre el análisis de causa raíz de un hallazgo, la
   solicitud la atiende el consumidor ``assistant_worker`` con el doble y deja un borrador marcado
   como tal, con citas.
7. **Exportación** (SUP-U05-21, 22 y 40): una zona y un periodo ``with_clips``; el consumidor
   ``export_worker`` compone el paquete real en LocalStack y la descarga por volúmenes lo trae a un
   directorio.
8. **Verificador fuera de la plataforma**: ``vigia_verify.py`` sacado del paquete a un directorio
   vacío y ejecutado en un contenedor ``python:3.10-slim`` sin red con ``-I -S``: ``intact``; con
   un bit alterado en un artefacto de hallazgos, ``broken``.
9. Las cadenas de la organización siguen íntegras con los registros de U-04 añadidos.

La semilla sale de ``VIGIA_LOAD_SEED`` o es aleatoria; se imprime y va en el informe
``u04-end-to-end-<perfil>.json`` con el resultado de cada paso. Solo datos generados. Las esperas
por condición tienen topes de minutos que nunca deciden un resultado.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import ssl
import threading
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.conformance.platform_target import Provisioner
from tests.integration.test_verifier_outside_platform import _extract_verifier, _run_python_310

# Fixtures de sesión de los perfiles de carga: la plataforma, el conjunto sellado sintético, la
# caché de clips y el directorio de informes.
from tests.load.conftest import (  # noqa: F401
    LoadTarget,
    clip_cache,
    load_reports,
    load_seed,
    load_target,
    sealed_dataset,
)
from tests.load.profiles import PROFILES, LoadProfile
from tests.load.test_load_ci import assert_functional, ledger_check, run_load
from tests.loop_unclosable_support import (
    ALTERNATIVE,
    COMPENSATORY,
    LICENSE,
)
from tests.loop_unclosable_support import REASON as REJECTION_REASON
from tests.resilience.harness import WALL, free_port, wait_until
from tests.verifier_bundle_support import flip_bit, rewrite_volume, volume_paths
from tests.writer_support import verify_ledger_chains
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.loop import registration as loop_registration
from vigia_platform.loop.adapters.assistant.fake import FAKE_MODEL, FakeAssistantProvider, Script
from vigia_platform.shared.context import Role
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.worker import compose_worker_runtime
from vigia_platform.shared.worker.main import WorkerConfig, WorkerProcess

pytestmark = pytest.mark.integration

PROFILE_VARIABLE: Final = "VIGIA_U04_E2E_PROFILE"
E2E_PROFILES: Final = ("u04-e2e", "ci-real-time")
"""``u04-e2e`` en el check de integración; ``ci-real-time`` (el de TASK-237) para el hito."""
HTTP_SECONDS: Final = 60.0
WORKER_START_SECONDS: Final = 180.0
SETTLE_SECONDS: Final = 600.0
"""Tope de las esperas por condición (proyección, borrador, paquete): nunca decide un resultado."""
CONCESSION_HEADER: Final = "X-Vigia-Concession"
REASON_TEXT: Final = "Coexistencia real observada en el acceso de la zona durante la carga"
ACTION_DESCRIPTION: Final = "Instalar un resguardo fijo en el acceso a la zona de carga"
EVIDENCE_DESCRIPTION: Final = "Resguardo fijo instalado y anclado al piso en el acceso"
CLOSURE_DESCRIPTION: Final = "Resguardo fijo verificado y anclado en el acceso de la zona"
POLICY_PDF: Final = b"%PDF-1.7 politica de hallazgos incerrables sintetica"


def e2e_profile() -> LoadProfile:
    """El perfil de ``VIGIA_U04_E2E_PROFILE`` (``u04-e2e`` si no está)."""
    name = os.environ.get(PROFILE_VARIABLE, "u04-e2e").strip() or "u04-e2e"
    if name not in E2E_PROFILES:
        raise ValueError(f"{PROFILE_VARIABLE} es uno de {E2E_PROFILES}, no {name!r}")
    return PROFILES[name]


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- vigia-worker con la raíz de producción en un hilo ---------------------------------------


@dataclass
class InProcessWorker:
    """``vigia-worker`` con ``compose_worker_runtime`` (todas las unidades registradas) y
    ``WorkerProcess``, en un hilo con su propio bucle, como ``InProcessApi`` para la API."""

    environ: Mapping[str, str]
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    _thread: threading.Thread | None = None
    _running: Any = None
    _stop: asyncio.Event | None = None

    def _call(self, awaitable: Any, timeout: float = WORKER_START_SECONDS) -> Any:
        return asyncio.run_coroutine_threadsafe(awaitable, self._loop).result(timeout=timeout)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="vigia-worker-en-proceso", daemon=True
        )
        self._thread.start()
        environ = {**self.environ, "VIGIA_WORKER_HEALTH_PORT": str(free_port())}
        config = WorkerConfig.from_environ(environ)
        runtime_config = RuntimeConfig.from_environ(environ)

        async def compose() -> tuple[WorkerProcess, asyncio.Event]:
            runtime = await compose_worker_runtime(config, runtime_config)
            return WorkerProcess(config, runtime), asyncio.Event()

        process, self._stop = self._call(compose())
        self._running = asyncio.run_coroutine_threadsafe(process.run(self._stop), self._loop)
        wait_until(
            lambda: process.started.is_set() or self._running.done(),
            timeout=WORKER_START_SECONDS,
            message="vigia-worker no terminó el arranque",
        )
        assert not self._running.done(), f"vigia-worker salió: {self._running.result()}"

    def stop(self) -> int | None:
        code: int | None = None
        if self._stop is not None and self._running is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            code = self._running.result(timeout=WORKER_START_SECONDS)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=60)
        self._loop.close()
        return code


@contextlib.contextmanager
def in_process_worker(environ: Mapping[str, str]) -> Iterator[InProcessWorker]:
    worker = InProcessWorker(environ)
    try:
        worker.start()
        yield worker
    finally:
        code = worker.stop()
        assert code in (None, 0), f"vigia-worker terminó con {code}"


# --- La coordinadora y sus peticiones ---------------------------------------------------------


@dataclass
class Console:
    """Una persona con sesión real detrás del balanceador ``app.``."""

    client: httpx.Client
    user_id: uuid.UUID
    verify: ssl.SSLContext
    """La CA de los balanceadores locales (también la de ``aws``, el de las URL prefirmadas)."""

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self.client.get(path, params=params or None)

    def post(self, path: str, body: Any = None) -> httpx.Response:
        return self.client.post(path, json=body)

    def ok(self, response: httpx.Response, status: int) -> dict[str, Any]:
        assert response.status_code == status, (response.request.url, response.text)
        document: dict[str, Any] = response.json()
        return document


def _console(
    target: LoadTarget, user: uuid.UUID, cookie: str, concession: uuid.UUID | None = None
) -> Console:
    headers = {"Cookie": f"{SESSION_COOKIE_NAME}={cookie}", "Sec-Fetch-Site": "same-origin"}
    if concession is not None:
        headers[CONCESSION_HEADER] = str(concession)
    verify = ssl.create_default_context(cafile=str(target.tls.ca_file))
    client = httpx.Client(
        base_url=target.app.url, headers=headers, verify=verify, timeout=HTTP_SECONDS
    )
    return Console(client, user, verify)


def _coordinator(target: LoadTarget, organization: uuid.UUID) -> tuple[uuid.UUID, str]:
    """Una coordinadora SST de la organización con su sesión (siembra como el arnés de carga)."""
    provisioner = Provisioner(
        target.stack, target.api, target.app.url, target.tls.ca_file, target.directory
    )
    user, statements = provisioner._user(organization, Role.COORDINATOR_SST)
    cookie, session = provisioner._session(organization, user)
    target.stack.admin([*statements, session])
    return user, cookie.value


def _installer_session(target: LoadTarget, concession: uuid.UUID) -> tuple[uuid.UUID, str]:
    """Una sesión **nueva** del instalador de la concesión de la flota.

    La que abre el aprovisionamiento lleva sin uso lo que dure la carga: a ritmo real (30 min)
    supera ``IDLE_TIMEOUT`` y responde 401, como debe; la consola real abre otra."""
    provisioner = Provisioner(
        target.stack, target.api, target.app.url, target.tls.ca_file, target.directory
    )
    (row,) = target.stack.fetch(
        "SELECT provider_user_id FROM identity.provider_concession WHERE concession_id = $1",
        concession,
    )
    user = uuid.UUID(str(row["provider_user_id"]))
    cookie, session = provisioner._session(target.stack.provider_organization_id, user)
    target.stack.admin([session])
    return user, cookie.value


# --- Lo que el expediente y la proyección dicen ------------------------------------------------


def _expected_subjects(target: LoadTarget, organization: uuid.UUID) -> dict[str, set[str]]:
    """Los episodios de los hallazgos y las detecciones a revisión que el expediente aceptó."""
    rows = target.stack.fetch(
        "SELECT record_type, content_json FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type IN ('finding_received', 'detection_for_review_received')",
        organization,
    )
    subjects: dict[str, set[str]] = {"finding": set(), "detection": set()}
    for row in rows:
        content = row["content_json"]
        content = json.loads(content) if isinstance(content, str) else content
        if row["record_type"] == "finding_received":
            subjects["finding"].add(str(content["episode"]["episode_id"]))
        else:
            subjects["detection"].add(str(content["detection_id"]))
    return subjects


def _projection(target: LoadTarget, organization: uuid.UUID) -> list[dict[str, Any]]:
    rows = target.stack.fetch(
        "SELECT finding_state_id, subject_kind, subject_id, zone_id, lifecycle_state"
        " FROM loop.finding_state WHERE organization_id = $1",
        organization,
    )
    return [dict(row) for row in rows]


def _inbox(console: Console, zones: tuple[uuid.UUID, ...]) -> list[dict[str, Any]]:
    """Toda la bandeja de las zonas, página a página (SUP-U05-01)."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = httpx.QueryParams({"zone_id": [str(zone) for zone in zones], "limit": 200})
        if cursor is not None:
            query = query.set("cursor", cursor)
        page = console.ok(console.client.get("/loop/findings", params=query), 200)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items


def _state(console: Console, finding_state_id: str) -> str:
    detail = console.ok(console.get(f"/loop/findings/{finding_state_id}"), 200)
    state: str = detail["lifecycle_state"]
    return state


# --- Los pasos ---------------------------------------------------------------------------------


def _resolve_review(console: Console) -> dict[str, Any]:
    queue = console.ok(console.get("/loop/review-queue"), 200)
    assert queue["items"], "la cola de revisión está vacía"
    detection = queue["items"][0]["detection_id"]
    resolution = console.ok(
        console.post(
            f"/loop/review-queue/{detection}/resolution",
            {
                "outcome": "review_confirmed",
                "reason_category": "real_coexistence",
                "reason_text": REASON_TEXT,
            },
        ),
        201,
    )
    assert resolution["lifecycle_state"] == "open", resolution
    return {"queue_items": len(queue["items"]), "detection_id": detection}


def _classify(console: Console, finding_state_id: str) -> dict[str, Any]:
    return console.ok(
        console.post(
            f"/loop/findings/{finding_state_id}/classifications",
            {
                "outcome": "confirmed",
                "reason_category": "real_coexistence",
                "reason_text": REASON_TEXT,
            },
        ),
        201,
    )


def _close_the_loop(console: Console, finding_state_id: str) -> dict[str, Any]:
    classification = _classify(console, finding_state_id)
    assert classification["lifecycle_state"] == "open", classification
    action = console.ok(
        console.post(
            f"/loop/findings/{finding_state_id}/actions",
            {
                "action_type": "engineering",
                "action_description": ACTION_DESCRIPTION,
                "assignee_user_id": str(console.user_id),
            },
        ),
        201,
    )
    assert action["lifecycle_state"] == "action_assigned", action
    evidence = console.ok(
        console.post(
            f"/loop/actions/{action['action_id']}/evidence",
            {
                "description": EVIDENCE_DESCRIPTION,
                "performed_at": _stamp(WALL.now() - dt.timedelta(minutes=5)),
                "performed_by_role": "Técnico de mantenimiento",
            },
        ),
        201,
    )
    closure = console.ok(
        console.post(
            f"/loop/findings/{finding_state_id}/closure",
            {"closure_description": CLOSURE_DESCRIPTION, "professional_license": LICENSE},
        ),
        201,
    )
    assert closure["lifecycle_state"] == "closed", closure
    assert closure["evidence_entry_ids"] == [evidence["evidence_entry_id"]], closure
    assert _state(console, finding_state_id) == "closed"
    return {
        "classification_id": classification["classification_id"],
        "action_id": action["action_id"],
        "evidence_entry_id": evidence["evidence_entry_id"],
        "closure_id": closure["closure_id"],
    }


def _load_plant_policy(installer: Console, plant_id: uuid.UUID) -> dict[str, Any]:
    """La política de hallazgos incerrables de la planta por las rutas de U-03 (§3.2)."""
    grant = installer.ok(
        installer.post(
            "/documents",
            {
                "plant_id": str(plant_id),
                "kind": "plant_policy",
                "content_type": "application/pdf",
                "size_bytes": len(POLICY_PDF),
                "sha256": hashlib.sha256(POLICY_PDF).hexdigest(),
            },
        ),
        201,
    )
    upload = grant["upload"]
    stored = httpx.request(
        upload["method"],
        upload["url"],
        content=POLICY_PDF,
        headers=upload["required_headers"],
        verify=installer.verify,
        timeout=HTTP_SECONDS,
    )
    assert stored.status_code in (200, 201), stored.text
    return installer.ok(
        installer.post(
            f"/plants/{plant_id}/policy",
            {
                "version": 1,
                "signed_at": _stamp(WALL.now() - dt.timedelta(days=1)),
                "signed_by_display_name": "Gerencia de planta sintética",
                "legal_opinion_reference": "CONCEPTO-JURIDICO-SINTETICO-1",
                "criteria_summary_es": "Criterios sintéticos de hallazgos sin alternativa segura",
                "document_ref": grant["document_ref"],
            },
        ),
        201,
    )


def _declare_unclosable(console: Console, finding_state_id: str) -> dict[str, Any]:
    classification = _classify(console, finding_state_id)
    assert classification["lifecycle_state"] == "open", classification
    declaration = console.ok(
        console.post(
            f"/loop/findings/{finding_state_id}/unclosable",
            {
                "evaluated_alternative": ALTERNATIVE,
                "rejection_reason": REJECTION_REASON,
                "responsible_user_id": str(console.user_id),
                "decision_date": (WALL.now() - dt.timedelta(days=1)).date().isoformat(),
                "compensatory_measure": COMPENSATORY,
                "professional_license": LICENSE,
            },
        ),
        201,
    )
    assert declaration["lifecycle_state"] == "unclosable", declaration
    assert _state(console, finding_state_id) == "unclosable"
    return {"declaration_id": declaration["declaration_id"]}


def _assistant_draft(console: Console, finding_state_id: str) -> dict[str, Any]:
    analysis = console.ok(
        console.post(f"/loop/findings/{finding_state_id}/root-cause-analyses"), 201
    )
    analysis_id = analysis["analysis_id"]
    request = console.ok(
        console.post(f"/loop/root-cause-analyses/{analysis_id}/assistant-requests"), 202
    )
    request_id = request["request_id"]

    def settled() -> dict[str, Any] | None:
        current = console.ok(console.get(f"/loop/assistant-requests/{request_id}"), 200)
        return None if current["assistant_state"] == "pending" else current

    answered = wait_until(settled, timeout=SETTLE_SECONDS, message="el asistente no respondió")
    assert answered is not None
    assert answered["assistant_state"] == "ready", answered
    draft = answered["draft"]
    assert draft is not None and draft["is_draft"] is True, answered
    assert draft["citations"], draft
    assert draft["model"] == FAKE_MODEL, draft
    return {
        "analysis_id": analysis_id,
        "request_id": request_id,
        "citations": len(draft["citations"]),
        "model": draft["model"],
    }


def _export(
    console: Console, plant_id: str, zone_id: str, start: dt.datetime, end: dt.datetime
) -> dict[str, Any]:
    requested = console.ok(
        console.post(
            "/accreditation/exports",
            {
                "plant_id": plant_id,
                "zone_ids": [zone_id],
                "period": {"from": _stamp(start), "to": _stamp(end)},
                "package_form": "with_clips",
            },
        ),
        202,
    )
    export_id = requested["export_id"]

    def finished() -> dict[str, Any] | None:
        current = console.ok(console.get(f"/accreditation/exports/{export_id}"), 200)
        return current if current["export_state"] in {"ready", "failed"} else None

    status = wait_until(finished, timeout=SETTLE_SECONDS, message="la exportación no terminó")
    assert status is not None
    assert status["export_state"] == "ready", status
    return status


def _download(console: Console, export_id: str, directory: Path) -> list[str]:
    """Los volúmenes del paquete por sus URL prefirmadas (SUP-U05-40), como un tercero."""
    listing = console.ok(console.get(f"/accreditation/exports/{export_id}/download"), 200)
    directory.mkdir()
    names: list[str] = []
    for volume in listing["volumes"]:
        response = httpx.get(
            volume["url"],
            verify=console.verify,
            timeout=HTTP_SECONDS,
        )
        assert response.status_code == 200, response.text
        assert len(response.content) == volume["size_bytes"]
        (directory / volume["name"]).write_bytes(response.content)
        names.append(volume["name"])
    return names


def _verify_outside(package: Path, root: Path) -> dict[str, Any]:
    """``vigia_verify.py`` del paquete en Python 3.10 limpio: intacto y con un bit alterado."""
    tercero = root / "tercero"
    tercero.mkdir(parents=True)
    _extract_verifier(package, tercero)
    assert sorted(child.name for child in tercero.iterdir()) == ["vigia_verify.py"]
    intact = root / "intacto"
    intact.mkdir()
    status, logs, document = _run_python_310(tercero, package, intact)
    assert status == 0, logs
    assert document["status"] == "intact" and document["broken"] is None, document

    altered = root / "alterado"
    shutil.copytree(package, altered)
    target = ""
    for volume in volume_paths(altered):
        content = volume.read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            candidates = sorted(name for name in archive.namelist() if name.startswith("findings/"))
            if not candidates:
                continue
            target = candidates[0]
            original = archive.read(target)
        volume.write_bytes(rewrite_volume(content, {target: flip_bit(original, 13)}))
        break
    assert target, "el paquete no lleva artefactos de hallazgos"
    broken_dir = root / "roto"
    broken_dir.mkdir()
    status, logs, broken = _run_python_310(tercero, altered, broken_dir)
    assert status == 1, logs
    assert broken["status"] == "broken", broken
    assert broken["broken"]["reason"] == "artifact_hash_mismatch", broken
    assert broken["broken"]["artifact"] == target, broken
    return {
        "intact": {
            "status": document["status"],
            "artifacts_verified": document["artifacts_verified"],
            "checkpoints_verified": document["checkpoints_verified"],
            "records_checked": document["records_checked"],
        },
        "altered": {"status": broken["status"], "reason": broken["broken"]["reason"]},
        "altered_artifact": target,
    }


# --- La prueba ---------------------------------------------------------------------------------


def test_u04_inbox_loop_assistant_and_export_over_the_simulated_node(
    load_target: LoadTarget,  # noqa: F811
    sealed_dataset: Path,  # noqa: F811
    clip_cache: Path,  # noqa: F811
    load_reports: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = e2e_profile()
    seed = load_seed()
    print(f"extremo a extremo de U-04: perfil {profile.name} semilla {seed}")
    steps: dict[str, Any] = {}
    provider = FakeAssistantProvider(Script.VALID_WITH_CITATIONS)
    monkeypatch.setattr(loop_registration, "assistant_provider", lambda *_, **__: provider)
    started_at = WALL.now()
    work = tmp_path / "nodos"
    work.mkdir()
    with in_process_worker(load_target.stack.environ):
        # 1. Los nodos simulados llenan la plataforma real por U-03.
        fleet, result, analysis, _ = run_load(
            load_target, profile, dataset=sealed_dataset, cache=clip_cache, work=work
        )
        ledger = ledger_check(load_target, fleet, set(result["journal"]["emitted"]))
        assert_functional(analysis, ledger)
        steps["nodes"] = {
            "emitted": analysis["emitted"],
            "accepted": analysis["accepted"],
            "ledger": {key: ledger[key] for key in ("records", "by_type", "chains_verified")},
        }
        organization = fleet.organization_id
        user, cookie = _coordinator(load_target, organization)
        console = _console(load_target, user, cookie)
        installer_user, installer_cookie = _installer_session(
            load_target, fleet.console.concession_id
        )
        installer = _console(
            load_target, installer_user, installer_cookie, fleet.console.concession_id
        )

        # 2. La bandeja: un elemento por episodio y por detección a revisión, ninguno más.
        expected = _expected_subjects(load_target, organization)
        assert len(expected["finding"]) >= 3 and expected["detection"], expected

        def projected() -> list[dict[str, Any]] | None:
            rows = _projection(load_target, organization)
            subjects = {
                kind: {str(row["subject_id"]) for row in rows if row["subject_kind"] == kind}
                for kind in ("finding", "detection")
            }
            return rows if subjects == expected else None

        rows = wait_until(
            projected, timeout=SETTLE_SECONDS, message="la proyección no alcanzó al expediente"
        )
        assert rows is not None
        inbox = _inbox(console, fleet.zone_ids)
        by_kind = {
            kind: [item for item in inbox if item["subject_kind"] == kind]
            for kind in ("finding", "detection")
        }
        assert len(by_kind["finding"]) == len(expected["finding"]), (by_kind, expected)
        assert len(by_kind["detection"]) == len(expected["detection"]), (by_kind, expected)
        assert len(inbox) == len({item["finding_state_id"] for item in inbox}) == len(rows)
        assert {item["finding_state_id"] for item in inbox} == {
            str(row["finding_state_id"]) for row in rows
        }
        steps["inbox"] = {
            "episodes": len(expected["finding"]),
            "detections_for_review": len(expected["detection"]),
            "items": len(inbox),
        }

        # 3. La cola de revisión.
        steps["review_queue"] = _resolve_review(console)

        # 4 a 6. El lazo sobre tres hallazgos de la misma zona si la hay.
        findings = sorted(
            by_kind["finding"], key=lambda item: (item["zone_id"], item["finding_state_id"])
        )
        closed, unclosable, analysed = findings[0], findings[1], findings[2]
        steps["closure"] = _close_the_loop(console, closed["finding_state_id"])
        policy = _load_plant_policy(installer, uuid.UUID(closed["plant_id"]))
        steps["plant_policy"] = {"policy_id": policy["policy_id"], "version": policy["version"]}
        steps["unclosable"] = _declare_unclosable(console, unclosable["finding_state_id"])
        steps["assistant"] = _assistant_draft(console, analysed["finding_state_id"])
        assert provider.call_count >= 1

        # 7. La exportación de una zona y un periodo, y su descarga por volúmenes.
        start = started_at.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
        end = WALL.now() + dt.timedelta(minutes=1)
        status = _export(console, closed["plant_id"], closed["zone_id"], start, end)
        package = tmp_path / "paquete"
        volumes = _download(console, status["export_id"], package)
        steps["export"] = {
            "export_id": status["export_id"],
            "zone_id": closed["zone_id"],
            "period": status["period"],
            "volumes": volumes,
            "manifest_summary": status["manifest_summary"],
        }

        # 8. El verificador del paquete fuera de la plataforma.
        steps["verifier"] = _verify_outside(package, tmp_path / "verificacion")

        # 9. Las cadenas siguen íntegras con los registros de U-04.
        chains = asyncio.run(verify_ledger_chains(load_target.stack.migrated, organization))
        assert chains and all(length > 0 for length in chains.values())
        steps["chains"] = {"verified": len(chains), "lengths": sorted(chains.values())}

    document = {
        "seed": seed,
        "profile": profile.describe(),
        "started_at": _stamp(started_at),
        "finished_at": _stamp(WALL.now()),
        "steps": steps,
        "result": "todos los pasos en verde",
    }
    text = json.dumps(document, ensure_ascii=False, indent=2, default=str)
    path = load_reports / f"u04-end-to-end-{profile.name}-{seed}.json"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"informe: {path}")
