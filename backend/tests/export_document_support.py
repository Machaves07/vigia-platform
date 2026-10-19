"""``ManifestInputs`` de ejemplo para las pruebas de ``accreditation.export.document`` (VIG-236).

Sin aleatoriedad ni hora del sistema: dos llamadas a ``sample_manifest_inputs`` devuelven
exactamente los mismos valores, en este proceso o en otro.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from vigia_platform.accreditation.manifest.model import (
    ActorSnapshot,
    AgreementEntry,
    AgreementSignatoryEntry,
    CheckpointEntry,
    CoverageZoneSummary,
    FindingSummary,
    ManifestInputs,
    OrganizationRef,
    PlantRef,
    RegressionPeriod,
    RevokedGate,
    StandardCited,
    ZoneEntry,
)
from vigia_platform.loop.domain.enums import PackageForm

ZONE_A: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
ZONE_B: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000000b")
PERIOD_FROM: datetime = datetime(2026, 1, 1, tzinfo=UTC)
PERIOD_TO: datetime = datetime(2026, 1, 2, tzinfo=UTC)
PERIOD_MS: int = 86_400_000


def _coverage(zone_id: uuid.UUID) -> CoverageZoneSummary:
    return CoverageZoneSummary(
        zone_id=zone_id,
        observable_ms=PERIOD_MS,
        degraded_ms=0,
        not_observable_ms=0,
        no_communication_ms=0,
        never_reported_ms=0,
        zone_not_active_ms=0,
        not_elapsed_ms=0,
        interval_count=1,
    )


def sample_manifest_inputs() -> ManifestInputs:
    """Un alcance de dos zonas con un poco de todo: hallazgos, acuerdo, regresión, revocación,
    estándar citado, punto de control y clave pública. Nada aquí sale del reloj del proceso."""
    zones = (
        ZoneEntry(
            zone_id=ZONE_A,
            code="ZA",
            name="Zona A",
            gate_intervals=(),
            agreements=(
                AgreementEntry(
                    agreement_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
                    record_id=uuid.UUID("00000000-0000-0000-0000-0000000000a2"),
                    status="approved",
                    approved_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    approved_by=uuid.UUID("00000000-0000-0000-0000-0000000000a3"),
                    state_in_period="current",
                    since=datetime(2026, 1, 1, 1, tzinfo=UTC),
                    replaces_agreement_id=None,
                    signatories=(
                        AgreementSignatoryEntry(
                            role="plant_manager",
                            user_id=uuid.UUID("00000000-0000-0000-0000-0000000000a4"),
                            confirmed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                        ),
                    ),
                    document_sha256=None,
                ),
            ),
            node_assignments=(),
            single_occupancy=True,
            aggregation_window_minutes=60,
        ),
        ZoneEntry(
            zone_id=ZONE_B,
            code="ZB",
            name="Zona B",
            gate_intervals=(),
            agreements=(),
            node_assignments=None,
            single_occupancy=False,
            aggregation_window_minutes=0,
        ),
    )
    findings = (
        FindingSummary(
            subject_id=uuid.UUID("00000000-0000-0000-0000-0000000000f1"),
            zone_id=ZONE_A,
            lifecycle_state="received",
            evidence_flagged=False,
        ),
        FindingSummary(
            subject_id=uuid.UUID("00000000-0000-0000-0000-0000000000f2"),
            zone_id=ZONE_B,
            lifecycle_state="closed",
            evidence_flagged=True,
        ),
    )
    return ManifestInputs(
        export_id=uuid.UUID("00000000-0000-0000-0000-0000000000e1"),
        package_form=PackageForm.WITHOUT_CLIPS.value,
        organization=OrganizationRef(
            organization_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            code="ORG",
            name="Organización de prueba",
        ),
        plant=PlantRef(
            plant_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            code="PLANT",
            name="Planta de prueba",
            country="CO",
            timezone="America/Bogota",
        ),
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
        generated_at=datetime(2026, 1, 2, 1, tzinfo=UTC),
        generated_by=ActorSnapshot(
            user_id="00000000-0000-0000-0000-0000000000u1",
            display_name="Persona de prueba",
            role_in_use="coordinator_sst",
        ),
        zones=zones,
        findings=findings,
        coverage=(_coverage(ZONE_A), _coverage(ZONE_B)),
        regression_periods=(
            RegressionPeriod(
                zone_id=ZONE_A,
                starts_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
                ends_at=datetime(2026, 1, 1, 3, tzinfo=UTC),
                cause="walk-test vencido",
            ),
        ),
        revoked_gates=(
            RevokedGate(
                zone_id=ZONE_B,
                since=datetime(2026, 1, 1, 4, tzinfo=UTC),
                until=None,
                reason_es="Gobernanza retiró el acuerdo de uso.",
                record_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
            ),
        ),
        standards_cited=(
            StandardCited(
                zone_id=ZONE_A,
                standard_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
                version=1,
                title_es="Estándar de prueba",
                declared_text="El texto declarado del estándar de prueba.",
                declared_text_sha256="a" * 64,
            ),
        ),
        clips=(),
        attachments=(),
        artifacts=(),
        absent=(),
        checkpoints=(
            CheckpointEntry(
                kind="ledger",
                plant_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
                entry_id=uuid.UUID("00000000-0000-0000-0000-000000000005"),
                sequence=1,
                entry_hash="b" * 64,
                content={},
            ),
        ),
        checkpoint_public_keys=(
            {
                "key_id": "key-1",
                "algorithm": "Ed25519",
                "public_key": "AA==",
                "status": "active",
                "valid_from": "2026-01-01T00:00:00.000Z",
                "valid_until": "2027-01-01T00:00:00.000Z",
            },
        ),
    )
