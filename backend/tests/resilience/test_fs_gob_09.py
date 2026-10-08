"""FS-GOB-09 · Publicación de la lista de revocación fallida (NFR-GOB-46, 48; PR-GOB-23;
escenario G-1; PAT-GOB-RES-02, RES-03).

Sobre la aplicación completa (``gob_platform``), con la tarea ``regenerate_revocation_list`` real
(``RevocationListService`` con el firmante de ``vigia-node-ca``: la **autoridad efímera** del
escenario, su clave en ``MemoryKms`` y su raíz en ``ca/root.pem`` del ``vigia-edge`` de LocalStack)
bajo el planificador real del worker (``PeriodicScheduler``, arrendamiento en
``shared.periodic_task``) y las métricas en memoria.

**Inyección**: el nodo de una zona productiva trabaja con normalidad (latido, catálogo y hallazgo
aceptados: cualquier caché de su estado estaría caliente); se revoca por la ruta de la consola
y, desde ese momento, el ``TrustStorePublisherPort`` tiene el **destino bloqueado** durante
**tres barridos** (60 s simulados entre uno y otro; ``BlockablePublisher``).

**Resultado esperado**:

- la primera capa queda intacta: el nodo revocado recibe ``node_revoked`` (401, permanente) en
  **cada** petición, antes, entre y después de los barridos, sea latido, catálogo, concesión o
  hallazgo; nada de lo que envía se escribe;
- cada barrido termina ``partial_failure`` con ``crl_publish_failed``; la marca
  ``revocation_list_dirty`` **persiste** (``dirty_generation`` por encima de
  ``published_generation`` y ``dirty_since`` sin tocar);
- **alarma**: ``revocation_list_publish_failed`` cuenta los tres fallos y
  ``periodic_task_duration_ms`` lleva la serie ``{task, result=failed}`` que vigila la alarma
  ``revocation-list-publish-failed`` (infrastructure-design §8.1);
- al restablecerse el destino, el **barrido siguiente** publica una lista firmada por la autoridad
  que contiene el certificado revocado y **limpia la marca**.

Solo datos generados.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any, Final

import pytest
from cryptography import x509

from tests.dispatch_support import metrics_with_reader
from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, gob_platform, ok
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.gob_support import BlockablePublisher, histogram_values, metric_sum
from tests.resilience.harness import scenario
from tests.worker_support import synchronize
from vigia_platform.fleet.adapters.ca.crl_signing import NodeCaRevocationListSigner
from vigia_platform.fleet.adapters.postgres.credential_store import PostgresCredentialStore
from vigia_platform.fleet.adapters.postgres.revocation_list_state_store import (
    PostgresRevocationListStateStore,
)
from vigia_platform.fleet.application.revocation_list_task import (
    TASK_NAME,
    RevocationListService,
    register_regenerate_revocation_list,
)
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.runtime.units import registered_units
from vigia_platform.shared.worker.leases import SqlLeaseStore, TaskOutcome
from vigia_platform.shared.worker.scheduler import PeriodicScheduler

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

SWEEPS: Final = 3
SWEEP_SECONDS: Final = 60
REASON: Final = "Equipo retirado de la línea 2 tras su sustitución"


@pytest.fixture(scope="module")
def gob(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    with gob_platform(postgres_endpoint, localstack_endpoint, "fs_gob_09") as platform:
        yield platform


def _requests(gob: GobPlatform, flow: Onboarding, zone: GobZone, pending: Any) -> dict[str, Any]:
    """Una petición de cada clase del nodo de ``zone`` con su certificado."""
    clip = {
        "clip_id": pending["cameras"][0]["clips"][0]["clip_id"],
        "camera_id": str(zone.cameras[0]),
        "zone_id": str(zone.zone_id),
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": "0" * 64,
        "size_bytes": 256,
        "duration_ms": 10_000,
        "purpose": "evidence",
    }
    return {
        "latido": flow.post_heartbeat(zone),
        "catálogo": gob.node_call(
            "GET", f"/api/nodes/zones/{zone.zone_id}/catalog", certificate=zone.cert
        ),
        "concesión": gob.node_call(
            "POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert, body=clip
        ),
        "hallazgo": flow.post_finding(zone, pending),
    }


def _codes(responses: dict[str, Any]) -> dict[str, tuple[int, str | None]]:
    return {
        what: (response.status_code, response.json().get("code"))
        for what, response in responses.items()
    }


def _state(gob: GobPlatform) -> dict[str, Any]:
    (row,) = gob.fetch(
        "SELECT dirty_generation, dirty_since, published_generation, entries"
        " FROM fleet.revocation_list_state"
    )
    return dict(row)


def test_fs_gob_09_revocation_list_publication_blocked_for_three_sweeps(gob: GobPlatform) -> None:
    with scenario(
        "FS-GOB-09",
        title="Publicación de la lista de revocación fallida",
        injection=(
            f"TrustStorePublisherPort con el destino bloqueado durante {SWEEPS} barridos tras una"
            " revocación"
        ),
        expected=(
            "node_revoked en cada petición; la marca revocation_list_dirty persiste; alarma"
            " revocation-list-publish-failed; al volver, el barrido siguiente publica y limpia"
        ),
    ) as run:
        metrics, reader = metrics_with_reader()
        publisher = BlockablePublisher()
        service = RevocationListService(
            states=PostgresRevocationListStateStore(),
            credentials=PostgresCredentialStore(),
            signer=NodeCaRevocationListSigner(kms=gob.kms, key_id=gob.kms.key_id, roots=gob.edge),
            publisher=publisher,
            clock=gob.clock,
            metrics=metrics,
        )
        # El catálogo del worker: los eventos de todas las unidades (ya en la base) y la tarea.
        catalog = OutboxCatalog()
        for unit in registered_units():
            unit.event_types(catalog.event_types)
        register_regenerate_revocation_list(catalog.periodic_tasks, service)
        database = gob.services.database
        contexts = gob.authz.contexts
        gob.run(synchronize(database, catalog, gob.clock))
        scheduler = PeriodicScheduler(
            database=database,
            registry=catalog.periodic_tasks,
            leases=SqlLeaseStore(database=database, contexts=contexts),
            contexts=contexts,
            clock=gob.clock,
            owner=f"fs-gob-09-{run.random.getrandbits(32):08x}",
            metrics=metrics,
        )

        def sweep() -> Any:
            gob.execute(
                "UPDATE shared.periodic_task SET next_run_at = $2, lease_owner = NULL,"
                " lease_until = NULL WHERE task_name = $1",
                TASK_NAME,
                gob.now() - dt.timedelta(seconds=1),
            )
            reports = gob.run(scheduler.run_pending())
            (report,) = [r for r in reports if r.task_name == TASK_NAME]
            return report

        flow = Onboarding(gob)
        zone = flow.productive_zone()
        # El nodo trabaja con normalidad: su estado se lee (y se cachearía) antes de revocarlo.
        working = {
            "latido": flow.post_heartbeat(zone),
            "catálogo": gob.node_call(
                "GET", f"/api/nodes/zones/{zone.zone_id}/catalog", certificate=zone.cert
            ),
            "hallazgo": flow.post_finding(zone, flow.finding(zone)),
        }
        pending = flow.finding(zone)  # con su clip ya subido, antes de la revocación
        findings_before = len(gob.records(zone.organization_id, "finding_received"))

        publisher.blocked = True
        ok(flow.as_installer(zone, "POST", f"/nodes/{zone.node}/revocation", {"reason_es": REASON}))
        marked = _state(gob)
        rounds = [_codes(_requests(gob, flow, zone, pending))]
        reports = []
        states = []
        for _ in range(SWEEPS):
            reports.append(sweep())
            states.append(_state(gob))
            rounds.append(_codes(_requests(gob, flow, zone, pending)))
            gob.advance(SWEEP_SECONDS)
        failed_counter = metric_sum(reader, MetricName.REVOCATION_LIST_PUBLISH_FAILED)
        failed_series = [
            (attributes, count)
            for attributes, count in histogram_values(reader, MetricName.PERIODIC_TASK_DURATION_MS)
            if attributes.get("task") == TASK_NAME
        ]

        publisher.blocked = False
        recovered = sweep()
        cleared = _state(gob)
        rounds.append(_codes(_requests(gob, flow, zone, pending)))
        published = publisher.published
        crl = x509.load_pem_x509_crl(published[-1].pem) if published else None
        listed = (
            crl is not None
            and crl.get_revoked_certificate_by_serial_number(zone.cert.serial_number) is not None
        )
        signed_by_authority = crl is not None and crl.is_signature_valid(gob.root.public_key())
        findings_after = len(gob.records(zone.organization_id, "finding_received"))
        run.observe(
            before_revocation={what: r.status_code for what, r in working.items()},
            mark_after_revocation={k: str(v) for k, v in marked.items()},
            node_requests=[{what: list(code) for what, code in r.items()} for r in rounds],
            sweeps=[{"outcome": r.outcome.value, "error": r.global_error_code} for r in reports],
            marks=[{k: str(v) for k, v in state.items()} for state in states],
            revocation_list_publish_failed=failed_counter,
            periodic_task_duration_ms=[[a, c] for a, c in failed_series],
            publisher_attempts=publisher.attempts,
            recovered={"outcome": recovered.outcome.value, "error": recovered.global_error_code},
            mark_after_recovery={k: str(v) for k, v in cleared.items()},
            revoked_certificate_listed=listed,
            signed_by_authority=signed_by_authority,
        )

        assert {what: r.status_code for what, r in working.items()} == {
            "latido": 200,
            "catálogo": 200,
            "hallazgo": 200,
        }
        # Primera capa: node_revoked en cada petición, en cada momento.
        for codes in rounds:
            assert codes == dict.fromkeys(codes, (401, "node_revoked")), codes
        assert findings_after == findings_before, "nada de lo que envía se escribe"
        # La marca persiste en los tres barridos fallidos.
        assert marked["dirty_generation"] > marked["published_generation"]
        assert marked["dirty_since"] is not None
        for report, state in zip(reports, states, strict=True):
            assert report.outcome is TaskOutcome.PARTIAL_FAILURE
            assert report.global_error_code == "crl_publish_failed"
            assert state["dirty_generation"] == marked["dirty_generation"]
            assert state["published_generation"] == marked["published_generation"]
            assert state["dirty_since"] == marked["dirty_since"]
        # La alarma: tres fallos contados y la serie que vigila revocation-list-publish-failed.
        assert failed_counter == SWEEPS
        assert failed_series == [({"task": TASK_NAME, "result": "failed"}, SWEEPS)]
        assert publisher.attempts == SWEEPS + 1
        # Al volver: el barrido siguiente publica la lista de la autoridad y limpia la marca.
        assert recovered.outcome is TaskOutcome.SUCCEEDED, recovered
        assert len(published) == 1 and listed and signed_by_authority
        assert cleared["published_generation"] == cleared["dirty_generation"]
        assert cleared["dirty_since"] is None
