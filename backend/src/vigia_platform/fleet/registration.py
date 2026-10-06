"""Registro de U-03 en la raíz de composición (TASK-227; LC-GOB-18; NFR-GOB-12, 20; A-52).

``shared.runtime.units`` declara las unidades ``catalog`` y ``fleet`` con lo que aquí se compone:

- **tipos y eventos**: los 26 tipos de registro (``register_catalog_record_types`` y
  ``register_fleet_record_types``, trece y trece) y los 15 eventos (``register_catalog_event_types``
  y ``register_fleet_event_types``, cinco y diez) de VIG-139, todos, también los que todavía no
  escribe ninguna ruta;
- **las siete tareas periódicas** de U-03 con sus nombres y cadencias definitivos (nota de
  cadencias de BL §2.6, NFR-GOB-12 y la nota de U-03 en ``domain-entities.md`` §4.3 de U-02), con
  las funciones ``register_*`` de cada módulo: ``catalog_tasks`` (``expire_walk_test_sessions``) y
  ``fleet_tasks`` (las otras seis). Las seis por organización heredan del planificador de U-02 el
  arrendamiento, una transacción y un contexto por organización, el cursor y las métricas
  (LC-NUC-24); ``regenerate_revocation_list`` es la única ``global`` (D-7);
- **la comprobación de arranque** (``missing_u03_registrations``, NFR-GOB-20 y BR-NUC-52): la
  composición contrasta lo registrado con ``U03_RECORD_TYPES``, ``U03_EVENT_TYPES`` y
  ``U03_TASKS``, escritos aquí **a mano** (no derivados de los registros): si falta uno, o una
  tarea cambió de cadencia, unidad o iteración, ``vigia-api``, ``vigia-worker`` y ``vigia-admin``
  no arrancan y la salida nombra lo que falta (``RegistrationIncomplete``).

Los tres procesos registran las siete tareas (``OutboxCatalog.synchronize`` no arranca si la base
tiene una tarea que el proceso no registra), pero solo el worker las ejecuta: lo que necesita un
depósito o una variable que el proceso no tiene (``vigia-evidence`` para ``mark_orphan_clips``;
``VIGIA_NODE_CA_KEY_ARN``, ``VIGIA_EDGE_BUCKET`` y ``VIGIA_NODE_TRUST_STORE_ARN`` para la lista de
revocación) se resuelve en la primera ejecución, y si falta, la ejecución falla nombrando la
variable (métrica y alarma de la tarea).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from vigia_platform.catalog.application.walk_test_expiry import (
    WalkTestExpirer,
    register_expire_walk_test_sessions,
)
from vigia_platform.fleet.adapters.ca.crl_signing import NodeCaRevocationListSigner
from vigia_platform.fleet.adapters.ca.trust_store_publisher import (
    CRL_OBJECT_KEY,
    TrustStorePublisher,
    build_elbv2_client,
)
from vigia_platform.fleet.adapters.postgres.credential_store import PostgresCredentialStore
from vigia_platform.fleet.adapters.postgres.revocation_list_state_store import (
    PostgresRevocationListStateStore,
)
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.enrollment_code_expiry import (
    EnrollmentCodeExpirer,
    register_expire_enrollment_codes,
)
from vigia_platform.fleet.application.expiring_certificates import (
    ExpiringCertificateAlerter,
    register_alert_expiring_certificates,
)
from vigia_platform.fleet.application.fleet_alarms import (
    AlarmDependencies,
    FleetAlarmEvaluator,
    register_evaluate_fleet_alarms,
)
from vigia_platform.fleet.application.mute_nodes import MuteDetector, register_detect_mute_nodes
from vigia_platform.fleet.application.orphan_clips import (
    OrphanClipSweeper,
    register_mark_orphan_clips,
)
from vigia_platform.fleet.application.revocation_list_task import (
    RevocationListService,
    register_regenerate_revocation_list,
)
from vigia_platform.fleet.domain.revocation_list import (
    PublishedRevocationList,
    RevocationListPlan,
    SignedRevocationList,
)
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.node_ca import ROOT_CERTIFICATE_KEY
from vigia_platform.shared.outbox.registries import (
    OutboxCatalog,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.storage import (
    PRESIGN_PUT_MAX_TTL,
    ObjectHead,
    PresignedRequest,
    S3Storage,
)

if TYPE_CHECKING:
    from vigia_platform.ledger.registry import RecordTypeRegistry
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "U03_EVENT_TYPES",
    "U03_RECORD_TYPES",
    "U03_TASKS",
    "catalog_tasks",
    "fleet_tasks",
    "missing_u03_registrations",
]

U03_RECORD_TYPES: Final = frozenset(
    {
        # catalog (13)
        "catalog_version_published",
        "standard_admission_test",
        "gate_state_changed",
        "mounting_gate_record",
        "use_agreement_signed",
        "commissioning_step",
        "walk_test_result",
        "plant_policy_signed",
        "occlusion_test_result",
        "walk_test_regression_marked",
        "walk_test_regression_cleared",
        "catalog_standard_retired",
        "single_occupancy_declared",
        # fleet (13)
        "finding_received",
        "detection_for_review_received",
        "observability_event_received",
        "node_communication_state_changed",
        "node_enrolled",
        "node_credential_rotated",
        "node_revoked",
        "update_result_received",
        "node_target_version_published",
        "node_decommissioned",
        "enrollment_code_issued",
        "enrollment_attempt_rejected",
        "ingest_rejected",
    }
)
"""Los 26 tipos de registro de U-03 (domain-entities §5; VIG-139)."""

U03_EVENT_TYPES: Final = frozenset(
    {
        # catalog (5)
        "gate_state_changed",
        "zone_activated",
        "catalog_updated",
        "regression_marked",
        "regression_cleared",
        # fleet (10)
        "finding_received",
        "detection_for_review_received",
        "observability_event_received",
        "node_enrolled",
        "node_revoked",
        "node_decommissioned",
        "target_version_published",
        "update_result_received",
        "fleet_alarm_raised",
        "fleet_alarm_cleared",
    }
)
"""Los 15 eventos de U-03 (VIG-139)."""

_EACH_ORGANIZATION: Final = TaskIteration.PER_ORGANIZATION

U03_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {
    "detect_mute_nodes": (Schedule.every(60), _EACH_ORGANIZATION),
    "evaluate_fleet_alarms": (Schedule.every(60), _EACH_ORGANIZATION),
    "expire_enrollment_codes": (Schedule.every(60), _EACH_ORGANIZATION),
    "mark_orphan_clips": (Schedule.every(3600), _EACH_ORGANIZATION),
    "expire_walk_test_sessions": (Schedule.daily(hour=5), _EACH_ORGANIZATION),
    "alert_expiring_certificates": (Schedule.daily(hour=3), _EACH_ORGANIZATION),
    # Barrido de 60 s con la marca; la regeneración diaria la decide el manejador (D-7).
    "regenerate_revocation_list": (Schedule.every(60), TaskIteration.GLOBAL),
}
"""Las siete tareas de U-03: nombre → (cadencia, iteración); todas con ``unit = U-03``."""


def missing_u03_registrations(
    record_types: RecordTypeRegistry, catalog: OutboxCatalog
) -> tuple[tuple[str, str], ...]:
    """Lo que falta de U-03 en los registros ya compuestos, como pares (clase, nombre).

    Una tarea con otra cadencia, otra unidad u otra iteración cuenta como ausente: no es la
    declarada (la alarma ``periodic-task-stale-<tarea>`` y el planificador dependen de ella).
    """
    missing = [
        ("record_type", name)
        for name in sorted(U03_RECORD_TYPES - set(record_types.record_types()))
    ]
    missing += [
        ("event_type", name)
        for name in sorted(U03_EVENT_TYPES - set(catalog.event_types.event_names()))
    ]
    for name, (schedule, iteration) in sorted(U03_TASKS.items()):
        task = catalog.periodic_tasks.get(name)
        if (
            task is None
            or task.unit is not ActorUnit.U03
            or task.schedule != schedule
            or task.iteration is not iteration
        ):
            missing.append(("periodic_task", name))
    return tuple(missing)


# --- Tareas ---------------------------------------------------------------------------------------


def catalog_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """``expire_walk_test_sessions`` (LC-GOB-06): diaria, por organización."""
    register_expire_walk_test_sessions(
        registry, WalkTestExpirer(clock=services.clock, metrics=services.metrics)
    )


def fleet_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    """Las seis tareas de ``fleet`` con sus manejadores (VIG-152, VIG-155, VIG-161 y TASK-227)."""
    alarms = AlarmDependencies(clock=services.clock, outbox=services.outbox)
    register_detect_mute_nodes(registry, MuteDetector(alarms, writer=services.writer))
    register_evaluate_fleet_alarms(registry, FleetAlarmEvaluator(alarms, audit=services.audit))
    register_alert_expiring_certificates(registry, ExpiringCertificateAlerter(alarms))
    register_expire_enrollment_codes(registry, EnrollmentCodeExpirer(clock=services.clock))
    register_mark_orphan_clips(
        registry,
        OrphanClipSweeper(
            database=services.database,
            store=ClipObjectStore(_EvidenceOnFirstUse(services)),
            clock=services.clock,
            metrics=services.metrics,
        ),
    )
    edge = _EdgeOnFirstUse(services)
    register_regenerate_revocation_list(
        registry,
        RevocationListService(
            states=PostgresRevocationListStateStore(),
            credentials=PostgresCredentialStore(),
            signer=_RevocationListSigner(services, edge),
            publisher=_RevocationListPublisher(services, edge),
            clock=services.clock,
            metrics=services.metrics,
        ),
    )


# --- Dependencias que solo resuelve el worker -----------------------------------------------------


class _EvidenceOnFirstUse:
    """``ClipStorage`` de ``vigia-evidence`` (``VIGIA_EVIDENCE_BUCKET``) resuelto al usarse."""

    def __init__(self, services: UnitServices) -> None:
        self._services = services

    async def head_object(self, key: str) -> ObjectHead | None:
        return await self._services.require_evidence().head_object(key)

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest:
        return await self._services.require_evidence().presign_put(
            key, content_type, checksum_sha256, required_headers, ttl
        )


def _config(services: UnitServices) -> RuntimeConfig:
    """La configuración del proceso; sin ella (pruebas sin entorno) no hay nada que construir."""
    if services.config is None:
        raise RuntimeConfigInvalid("VIGIA_ENVIRONMENT", "ausente: este proceso no se configuró")
    return services.config


def _require(services: UnitServices, field: str) -> str:
    """El valor de ``field`` en la configuración; ``RuntimeConfigInvalid`` con su variable."""
    value: str = _config(services).require(field)
    return value


class _EdgeOnFirstUse:
    """El depósito ``vigia-edge`` (``VIGIA_EDGE_BUCKET``), construido en la primera ejecución."""

    def __init__(self, services: UnitServices) -> None:
        self._services = services
        self._storage: S3Storage | None = None

    @property
    def bucket(self) -> str:
        return _require(self._services, "edge_bucket")

    def storage(self) -> S3Storage:
        if self._storage is None:
            # ``runtime.core`` importa la raíz, que importa este módulo: se resuelve al usarse.
            from vigia_platform.shared.runtime.core import s3_storage

            config = _config(self._services)
            self._storage = s3_storage(config, self.bucket, self._services.clock)
        return self._storage


class _RevocationListSigner:
    """``NodeCaRevocationListSigner`` con ``VIGIA_NODE_CA_KEY_ARN`` y ``ca/root.pem`` de
    ``vigia-edge``, construido en la primera firma."""

    def __init__(self, services: UnitServices, edge: _EdgeOnFirstUse) -> None:
        self._services = services
        self._edge = edge
        self._signer: NodeCaRevocationListSigner | None = None

    async def sign(self, plan: RevocationListPlan) -> SignedRevocationList:
        if self._signer is None:
            key_id = _require(self._services, "node_ca_key_arn")
            self._signer = NodeCaRevocationListSigner(
                kms=self._services.kms,
                key_id=key_id,
                roots=self._edge.storage(),
                root_key=ROOT_CERTIFICATE_KEY,
            )
        return await self._signer.sign(plan)


class _RevocationListPublisher:
    """``TrustStorePublisher`` sobre ``vigia-edge`` y ``VIGIA_NODE_TRUST_STORE_ARN``, construido en
    la primera publicación."""

    def __init__(self, services: UnitServices, edge: _EdgeOnFirstUse) -> None:
        self._services = services
        self._edge = edge
        self._publisher: TrustStorePublisher | None = None

    async def publish(self, revocation_list: SignedRevocationList) -> PublishedRevocationList:
        if self._publisher is None:
            trust_store = _require(self._services, "node_trust_store_arn")
            config = _config(self._services)
            self._publisher = TrustStorePublisher(
                storage=self._edge.storage(),
                elb=build_elbv2_client(
                    region=config.aws_region, endpoint_url=config.aws_endpoint_url
                ),
                trust_store_arn=trust_store,
                bucket=self._edge.bucket,
                object_key=config.crl_key if config.crl_key is not None else CRL_OBJECT_KEY,
            )
        return await self._publisher.publish(revocation_list)
