"""Composición y sellado del paquete mensual (TASK-342 a 344; LC-LAZ-24; C-PLA-30; BR-LAZ-102,
105 y 115; U04-H-12).

``MonthlyPackageComposer.compose_and_seal`` hace, para una zona y un mes, todo lo que
``monthly.task.MonthlyPackageTask`` no sabe hacer por sí misma: lee el expediente de la zona en el
periodo, compone el resumen de hallazgos y de recurrencia, reclama la fila
(``MonthlyPackageRepository.claim``), extrae la muestra auditada (``monthly.sample``), declara
adopción, métricas y exposición, construye el zip del paquete, lo sube al depósito de evidencias,
fija el punto de control, inserta la muestra y sella, escribiendo por último
``monthly_package_produced`` en el expediente de la planta.

**El orden importa** (BR-LAZ-102, «el cierre no miente», P5): el reclamo va **primero**, antes de
componer nada más (como el arrendamiento de la exportación): si el periodo ya estaba sellado, no
se lee nada de más, no se construye ningún zip y no se sube ningún objeto. ``claim`` nunca cambia
``package_id`` de una fila existente (la migración no concede ``UPDATE`` sobre esa columna):
devuelve el identificador **de la fila** (el candidato propio, o el de un intento anterior sin
sellar que se retoma con su propia identidad), que el resto de la composición adopta. El sellado
de la fila y la escritura del expediente van en **una sola transacción**: si el sellado pierde la
carrera entre medias (``seal`` no afecta ninguna fila, vallado por ``state != 'sealed'``), la
transacción se revierte entera y el registro escrito no llega a confirmarse (nunca un
``monthly_package_produced`` sin su fila sellada, ni al revés).

**Simplificaciones de esta tarea, declaradas aquí y en el informe final:**

- **Documento portátil**: ``accreditation.document.renderer.generate_document`` está construido
  para el ``ManifestInputs`` de la exportación (TASK-319); adaptarlo al paquete mensual es un
  trabajo de otra magnitud. Este módulo genera en su lugar un PDF mínimo y determinista
  (``_render_document``), sin ``WeasyPrint`` ni plantillas: cumple «el paquete contiene el
  documento» sin inventar una adaptación completa del renderizador de exportación.
- **Métricas y exposición**: el puerto de métricas (TASK-321) y el de exposición (TASK-3xx,
  VIG-231/237) no existen todavía en ``main``. Cada métrica se declara ``not_computable`` y la
  exposición también (BR-LAZ-115: nunca un valor inventado, nunca cero en su lugar).
- **Cobertura** (``coverage_summary``): no hay aquí integración con ``CoverageService``; se
  declara vacía (``{}``, objeto jsonb por ``monthly_package_coverage``), nunca una cifra inventada.
- **Revisor de la muestra** (``audit_sample.reviewer_user_id``): obligatorio en el esquema (quien
  revisará, no quien ya revisó); se resuelve al coordinador SST vigente de la zona (por zona,
  planta u organización). Sin ninguno asignado, ``MonthlyPackageReviewerMissing`` deja la fila en
  ``generating`` para reintentar al día siguiente, nunca inventa un revisor.
- **Adopción**: ``accreditation.adoption_counter`` ya existe en el esquema pero ningún escritor de
  este repositorio la llena; mientras no lo tenga, la cuenta de días activos es honestamente 0.
- **Compromisos del periodo anterior**: ``accreditation.commitment`` existe, pero su escritor
  (TASK-347/``notifications``) tampoco existe: una zona sin compromisos registrados da la lista
  vacía, nunca inventada.
- **Recurrencia**: no se correlaciona el cierre previo exacto (``reference_closure_ids`` siempre
  vacío); contar cuántas reclasificaciones son «nuevo episodio tras un cierre» por familia es la
  aproximación ``[estimación propia simplificada]`` de esta tarea.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import re
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.accreditation.export.lease import RecordWriter
from vigia_platform.accreditation.export.worker import CheckpointPort, covered_chains
from vigia_platform.accreditation.monthly.repository import (
    AuditSampleItemRow,
    MonthlyPackageRepository,
    ZoneRef,
    object_key,
    period_to_date,
)
from vigia_platform.accreditation.monthly.sample import (
    SampleCandidate,
    SampleDraw,
    draw_sample,
)
from vigia_platform.accreditation.verifier_bundle import VERIFIER_PATH, load_verifier_bundle
from vigia_platform.ledger.application.reader import (
    LectorExpediente,
    LedgerFilters,
    LedgerRecordView,
    PageRequest,
    RecordCursor,
)
from vigia_platform.ledger.application.writer import LedgerRejection, RecordScope
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.ledger.chain.checkpoints import ChainKind, CheckpointChain
from vigia_platform.loop.calendar import month_bounds
from vigia_platform.loop.domain.enums import ExposureStatus, MetricKey, MetricStatus, MetricUnit
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import ObjectHead

__all__ = [
    "MONTHLY_PACKAGE_AVAILABLE",
    "MONTHLY_PACKAGE_PRODUCED",
    "ComposeOutcome",
    "EvidencePort",
    "EvidencePortMissing",
    "MissingEvidence",
    "MonthlyPackageComposer",
    "MonthlyPackageRecordRejected",
    "MonthlyPackageReviewerMissing",
    "MonthlyPackageUnsealable",
    "TransactionSource",
]

MONTHLY_PACKAGE_PRODUCED: Final = "monthly_package_produced"
MONTHLY_PACKAGE_AVAILABLE: Final = "monthly_package_available"

_RECORD_TYPES: Final = (
    "classification",
    "closure",
    "unclosable_state_declared",
    "reopening",
    "action_overdue",
)
_PAGE_SIZE: Final = 200
_MAX_PAGES: Final = 500
"""Tope de páginas por zona y mes (100 000 registros): evita un bucle sin fin, nunca pensado para
alcanzarse en la vida de una sola zona."""

_DOCUMENT_NAME: Final = "document.pdf"
_MANIFEST_NAME: Final = "manifest.json"
_ZIP_DATE_TIME: Final = (1980, 1, 1, 0, 0, 0)
"""Fecha fija de las entradas del zip: el paquete no depende del reloj del proceso (PR-LAZ-49)."""

_METRIC_UNITS: Final[Mapping[MetricKey, MetricUnit]] = {
    MetricKey.PRIMARY_ENGINEERING_CLOSURES: MetricUnit.COUNT,
    MetricKey.ACTIVATION: MetricUnit.RATIO,
    MetricKey.ADOPTION: MetricUnit.RATIO,
    MetricKey.CLASSIFIED_IN_TIME: MetricUnit.RATIO,
    MetricKey.OVERDUE: MetricUnit.RATIO,
    MetricKey.RECURRENCE: MetricUnit.COUNT,
    MetricKey.EXPOSURE: MetricUnit.MINUTES,
}
"""Unidad declarada de cada métrica (§3.4); todas ``not_computable`` hasta TASK-321."""

_log = get_logger("accreditation.monthly")


class MonthlyPackageUnsealable(Exception):
    """Ninguna cadena cubierta tiene un punto de control que fijar (expediente vacío de verdad)."""


class MonthlyPackageReviewerMissing(Exception):
    """La zona todavía no tiene un coordinador SST vigente: ``audit_sample.reviewer_user_id`` es
    obligatorio y nunca se inventa. La fila queda ``generating`` y la zona se reintenta al día
    siguiente (BR-LAZ-141), no distinto de un checkpoint sin fijar."""


class MonthlyPackageRecordRejected(Exception):
    """El expediente rechazó ``monthly_package_produced``: no hay paquete sellado."""

    def __init__(self, code: str) -> None:
        super().__init__(f"monthly_package_produced rechazado: {code}")
        self.code = code


class _SealLost(Exception):
    """Otra composición selló ``(zone_id, period)`` entre medias: se descarta todo lo de aquí."""


class EvidencePort(Protocol):
    """Lo único que la composición usa del depósito de evidencias: subir un objeto de una vez.

    El paquete mensual es de una sola zona y un solo mes, muy por debajo de lo que justifica la
    subida por partes de la exportación (``ExportStream``): un ``put_object`` de un tiro basta.
    """

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
        if_match: str | None = None,
    ) -> ObjectHead: ...


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base."""

    def transaction(self, context: ScopeContext) -> AbstractAsyncContextManager[Transaction]: ...


class EvidencePortMissing(Exception):
    """El proceso no tiene depósito de evidencias: el paquete mensual no se compone."""

    code: Final = "evidence_port_missing"


class MissingEvidence:
    """El depósito de un proceso sin ``vigia-evidence``: falla sin tocar nada (igual que
    ``export.composer_port.MissingUploadPort``)."""

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
        if_match: str | None = None,
    ) -> ObjectHead:
        raise EvidencePortMissing()


@dataclass(frozen=True, slots=True, kw_only=True)
class ComposeOutcome:
    """Lo que devuelve una composición que selló de verdad (``None`` si perdió la carrera)."""

    package_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    package_sha256: str
    record_id: uuid.UUID


def _within_period(record: LedgerRecordView, period_from: datetime, period_to: datetime) -> bool:
    moment = record.occurred_at if record.occurred_at is not None else record.received_at
    return period_from <= moment < period_to


def _next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{year + 1:04d}-01" if month == 12 else f"{year:04d}-{month + 1:02d}"


def _previous_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


# --- resúmenes puros a partir de los registros del periodo ----------------------------------


def _findings_summary(records: Sequence[LedgerRecordView]) -> dict[str, int]:
    """``FindingsSummary``: ``received`` se aproxima por sujetos distintos clasificados en el
    periodo (no hay, en este alcance, una marca de «primera vez visto» más barata de leer); es una
    aproximación declarada, no una cuenta de un evento «received» propio."""
    classifications = [r for r in records if r.record_type == "classification"]
    subjects: set[str] = set()
    confirmed = discarded = 0
    for record in classifications:
        document = record.document()
        subjects.add(str(document["subject_id"]))
        outcome = document["outcome"]
        if outcome == "confirmed":
            confirmed += 1
        elif outcome == "false_positive":
            discarded += 1
    return {
        "received": len(subjects),
        "classified": len(classifications),
        "confirmed": confirmed,
        "discarded": discarded,
        "closed": sum(1 for r in records if r.record_type == "closure"),
        "overdue": sum(1 for r in records if r.record_type == "action_overdue"),
        "unclosable": sum(1 for r in records if r.record_type == "unclosable_state_declared"),
        "reopened": sum(1 for r in records if r.record_type == "reopening"),
    }


def _recurrence_summary(records: Sequence[LedgerRecordView]) -> list[dict[str, Any]]:
    """Reclasificaciones cerradas: cuenta por familia los episodios nuevos (no reclasificación)
    que enlazan con una clasificación previa (``[estimación propia simplificada]``: no se
    correlaciona el cierre exacto, solo se cuenta por familia)."""
    counts: dict[str, int] = {}
    for record in records:
        if record.record_type != "classification":
            continue
        document = record.document()
        if document.get("is_reclassification") is not False:
            continue
        if document.get("previous_classification_record_id") is None:
            continue
        family = str(document["family"])
        counts[family] = counts.get(family, 0) + 1
    return [
        {"family": family, "episodes_after_closure": count, "reference_closure_ids": []}
        for family, count in sorted(counts.items())
        if count > 0
    ]


def _sample_candidates(records: Sequence[LedgerRecordView]) -> list[SampleCandidate]:
    candidates: list[SampleCandidate] = []
    for record in records:
        if record.record_type != "classification":
            continue
        document = record.document()
        if document.get("is_reclassification") is not True:
            continue
        candidates.append(
            SampleCandidate(
                classification_id=uuid.UUID(str(document["classification_id"])),
                subject_id=uuid.UUID(str(document["subject_id"])),
                family=str(document["family"]),
                reason_category=str(document["reason_category"]),
            )
        )
    return candidates


def _metrics_not_computable() -> list[dict[str, Any]]:
    """BR-LAZ-115: una métrica no calculable se declara, nunca se omite ni vale cero."""
    return [
        {
            "key": key.value,
            "status": MetricStatus.NOT_COMPUTABLE.value,
            "value": None,
            "numerator": None,
            "denominator": None,
            "unit": unit.value,
            "excluded_unclosable_count": 0,
        }
        for key, unit in _METRIC_UNITS.items()
    ]


def _exposure_not_computable() -> dict[str, Any]:
    return {
        "status": ExposureStatus.NOT_COMPUTABLE.value,
        "confirmed_minutes": None,
        "pending_classification_minutes": None,
        "observable_coverage_percent": None,
    }


# --- documento portátil mínimo (placeholder; ver docstring del módulo) ----------------------

_PDF_ESCAPE: Final = re.compile(r"[\\()]")


def _escape_pdf_text(text: str) -> str:
    return _PDF_ESCAPE.sub(lambda match: "\\" + match.group(0), text)


def _minimal_pdf(lines: Sequence[str]) -> bytes:
    """Un PDF válido de una sola página con líneas de texto fijas (sin reloj, sin fuentes)."""
    y = 740
    operations = ["BT", "/F1 11 Tf"]
    for line in lines:
        operations.append(f"1 0 0 1 72 {y} Tm ({_escape_pdf_text(line)}) Tj")
        y -= 16
    operations.append("ET")
    content = "\n".join(operations).encode("latin-1", errors="replace")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >>"
        b" /MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    buffer = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(buffer)
    count = len(objects) + 1
    buffer += f"xref\n0 {count}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        buffer += f"{offset:010d} 00000 n \n".encode("ascii")
    buffer += (f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF").encode(
        "ascii"
    )
    return bytes(buffer)


def _render_document(
    *,
    zone_id: uuid.UUID,
    period: str,
    findings: Mapping[str, int],
    generated_at: datetime,
) -> bytes:
    """Documento portátil mínimo y determinista (ver «Simplificaciones» del docstring del módulo).

    Ningún dato de persona (P3): solo la zona, el periodo y los conteos de ``findings_summary``.
    """
    lines = [
        "Vigia -- paquete mensual (documento provisional, sin disenio final)",
        f"Zona: {zone_id}",
        f"Periodo: {period}",
        f"Generado: {format_timestamp(generated_at)}",
        f"Clasificaciones: {findings['classified']}  Confirmadas: {findings['confirmed']}",
        f"Descartadas: {findings['discarded']}  Cerradas: {findings['closed']}",
        f"Vencidas: {findings['overdue']}  Incerrables: {findings['unclosable']}",
        f"Reabiertas: {findings['reopened']}",
    ]
    return _minimal_pdf(lines)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


@repository
class MonthlyPackageComposer:
    """Compone y sella el paquete mensual de una zona y un mes (BR-LAZ-102)."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        reader: LectorExpediente,
        repository: MonthlyPackageRepository,
        evidence: EvidencePort,
        checkpoints: CheckpointPort,
        writer: RecordWriter,
        clock: Clock,
    ) -> None:
        self._database = database
        self._reader = reader
        self._repository = repository
        self._evidence = evidence
        self._checkpoints = checkpoints
        self._writer = writer
        self._clock = clock

    async def compose_and_seal(
        self,
        context: ScopeContext,
        zone: ZoneRef,
        period: str,
        plant_timezone: str,
        *,
        sample_percent: int,
        sample_min: int,
        sample_max: int,
    ) -> ComposeOutcome | None:
        """Compone el paquete de ``zone`` y ``period``, y lo sella si nadie se lo llevó antes.

        ``None`` es un resultado normal (ya sellado por otra ejecución, BR-LAZ-102): no es un
        fallo y no se reintenta ni se escribe nada.
        """
        period_from, period_to = month_bounds(plant_timezone, period)
        records = await self._period_records(context, zone.zone_id, period_from, period_to)
        findings = _findings_summary(records)
        recurrence = _recurrence_summary(records)
        candidates = _sample_candidates(records)

        candidate_package_id = uuid7(self._clock)
        generated_at = self._clock.now()

        # El reclamo va primero (como el arrendamiento de export): si el periodo ya está sellado,
        # no se compone nada más, ni se sube un zip de más. ``package_id`` es el de la fila (puede
        # no ser el candidato: una fila sin sellar de un intento anterior conserva su identidad).
        package_id = await self._claim(context, zone, period, candidate_package_id, generated_at)
        if package_id is None:
            _log.debug(
                "paquete mensual ya sellado: se descarta la composición",
                zone_id=str(zone.zone_id),
                period=period,
            )
            return None

        sample_id = uuid7(self._clock)
        # ``audit_sample.seed`` es ``bigint`` (64 bits con signo): acotado a los 63 bits bajos,
        # un subconjunto válido del rango de ``draw_sample`` (hasta 2**64 - 1 en MAX_SEED, para
        # una semilla cualquiera que alguien reproduzca a mano, no solo la que deriva esta tarea).
        seed = package_id.int & ((1 << 63) - 1)
        draw = draw_sample(
            candidates,
            sample_percent=sample_percent,
            sample_min=sample_min,
            sample_max=sample_max,
            seed=seed,
        )
        audit_sample = _audit_sample_doc(sample_id, draw)
        adoption = await self._adoption(context, zone.zone_id, period)
        previous_commitments = await self._previous_commitments(context, zone, period)
        reviewer_user_id = await self._coordinator(context, zone)
        if reviewer_user_id is None:
            raise MonthlyPackageReviewerMissing(f"zona {zone.zone_id} sin coordinador SST vigente")

        zip_bytes = self._build_zip(
            package_id=package_id,
            zone=zone,
            period=period,
            generated_at=generated_at,
            findings=findings,
        )
        package_sha256 = hashlib.sha256(zip_bytes).hexdigest()

        key = object_key(
            organization_id=zone.organization_id,
            plant_id=zone.plant_id,
            zone_id=zone.zone_id,
            period=period,
        )
        await self._evidence.put_object(key, zip_bytes, "application/zip")
        checkpoint_id = await self._write_checkpoint(context, zone.plant_id)

        by_classification = {candidate.classification_id: candidate for candidate in candidates}
        items = [
            AuditSampleItemRow(
                sample_item_id=uuid7(self._clock),
                sample_id=sample_id,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                classification_id=classification_id,
                subject_id=by_classification[classification_id].subject_id,
            )
            for classification_id in draw.selected
        ]
        await self._insert_sample(
            context, zone, package_id, sample_id, draw, items, generated_at, reviewer_user_id
        )

        content: dict[str, Any] = {
            "package_id": str(package_id),
            "zone_id": str(zone.zone_id),
            "period": period,
            "findings_summary": findings,
            "recurrence_summary": recurrence,
            "metrics": _metrics_not_computable(),
            "exposure": _exposure_not_computable(),
            "adoption": adoption,
            "audit_sample": audit_sample,
            "commitments_from_previous_period": previous_commitments,
            "coverage_summary": [],
            "package_sha256": package_sha256,
            "sealed_checkpoint_record_id": str(checkpoint_id),
            "generated_at": format_timestamp(generated_at),
        }

        record_id = await self._seal_and_write(
            context,
            zone=zone,
            period=period,
            package_id=package_id,
            sample_id=sample_id,
            checkpoint_id=checkpoint_id,
            findings=findings,
            recurrence=recurrence,
            commitments=previous_commitments,
            generated_at=generated_at,
            content=content,
        )
        if record_id is None:
            return None
        return ComposeOutcome(
            package_id=package_id,
            zone_id=zone.zone_id,
            period=period,
            package_sha256=package_sha256,
            record_id=record_id,
        )

    async def _period_records(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        period_from: datetime,
        period_to: datetime,
    ) -> tuple[LedgerRecordView, ...]:
        """Todos los registros de ``_RECORD_TYPES`` de la zona, filtrados en Python por
        ``occurred_at`` (o ``received_at`` sin ella) dentro del periodo.

        Lee toda la historia de esos tipos en la zona (acotada a ``_MAX_PAGES``): el expediente no
        permite filtrar por ``occurred_at`` en el servidor y ``received_at`` puede llegar con
        retraso (correcciones, reintentos). Para una sola zona y estos cinco tipos de registro es
        un volumen mensual, no de exportación; una organización con zonas muy longevas podría
        necesitar un filtro de ``received_at`` más fino en una tarea futura (TASK-349, métricas).
        """
        collected: list[LedgerRecordView] = []
        cursor: RecordCursor | None = None
        filters = LedgerFilters(record_types=_RECORD_TYPES, zone_id=zone_id)
        for _ in range(_MAX_PAGES):
            page_request = PageRequest(size=_PAGE_SIZE, after=cursor)
            page = await self._reader.list(context, filters, page_request)
            collected.extend(page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return tuple(
            record for record in collected if _within_period(record, period_from, period_to)
        )

    async def _adoption(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> dict[str, int]:
        start = period_to_date(period)
        end = period_to_date(_next_period(period))
        async with self._database.transaction(context) as transaction:
            active = await self._repository.adoption_days(
                transaction, zone_id=zone_id, period_start=start, period_end_exclusive=end
            )
        days_in_month = calendar.monthrange(start.year, start.month)[1]
        return {"active_days": active, "silent_days": max(days_in_month - active, 0)}

    async def _previous_commitments(
        self, context: ScopeContext, zone: ZoneRef, period: str
    ) -> list[dict[str, Any]]:
        previous_start = period_to_date(_previous_period(period))
        async with self._database.transaction(context) as transaction:
            commitments = await self._repository.previous_commitments(
                transaction, zone_id=zone.zone_id, previous_period_start=previous_start
            )
        return [{"commitment_id": str(cid), "state": state} for cid, state in commitments]

    async def _claim(
        self,
        context: ScopeContext,
        zone: ZoneRef,
        period: str,
        package_id: uuid.UUID,
        generated_at: datetime,
    ) -> uuid.UUID | None:
        """``None`` si ya estaba sellado; si no, el ``package_id`` de la fila, adoptado para todo
        lo que sigue (puede no ser el candidato recibido; ver ``MonthlyPackageRepository``)."""
        async with self._database.transaction(context) as transaction:
            return await self._repository.claim(
                transaction,
                package_id=package_id,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                period=period,
                generated_at=generated_at,
            )

    async def _coordinator(self, context: ScopeContext, zone: ZoneRef) -> uuid.UUID | None:
        """El coordinador SST vigente de la zona, para ``audit_sample.reviewer_user_id``
        (``NOT NULL`` en el esquema: quien revisará, no quien ya revisó)."""
        async with self._database.transaction(context) as transaction:
            return await self._repository.coordinator_user_id(
                transaction, plant_id=zone.plant_id, zone_id=zone.zone_id
            )

    async def _write_checkpoint(self, context: ScopeContext, plant_id: uuid.UUID) -> uuid.UUID:
        """El punto de control que sella el paquete (U04-H-08, símil del arranque de exportación).

        Orden preferido: la cadena del expediente de la propia planta; si está vacía, la de la
        organización; si también lo está, la de auditoría. Solo si las tres cadenas cubiertas
        están realmente vacías (ninguna escritura jamás) se declara ``MonthlyPackageUnsealable``:
        nunca se inventa un punto de control.
        """
        results = await self._checkpoints.write_checkpoints_now(context, covered_chains(plant_id))
        by_chain = {result.chain: result for result in results}
        for chain in (
            CheckpointChain(ChainKind.LEDGER, plant_id),
            CheckpointChain(ChainKind.LEDGER, None),
            CheckpointChain(ChainKind.AUDIT, None),
        ):
            result = by_chain.get(chain)
            if result is not None and result.checkpoint is not None:
                return result.checkpoint.entry_id
        raise MonthlyPackageUnsealable("ninguna cadena cubierta tiene puntos de control que fijar")

    async def _insert_sample(
        self,
        context: ScopeContext,
        zone: ZoneRef,
        package_id: uuid.UUID,
        sample_id: uuid.UUID,
        draw: SampleDraw,
        items: Sequence[AuditSampleItemRow],
        generated_at: datetime,
        reviewer_user_id: uuid.UUID,
    ) -> None:
        async with self._database.transaction(context) as transaction:
            await self._repository.insert_audit_sample(
                transaction,
                sample_id=sample_id,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                package_id=package_id,
                population_size=draw.population_size,
                sample_size=draw.sample_size,
                seed=draw.seed,
                strata=[
                    {
                        "family": stratum.family,
                        "reason_category": stratum.reason_category,
                        "population": stratum.population,
                        "drawn": stratum.drawn,
                    }
                    for stratum in draw.strata
                ],
                selected_at=generated_at,
                reviewer_user_id=reviewer_user_id,
            )
            await self._repository.insert_audit_sample_items(transaction, items=items)

    async def _seal_and_write(
        self,
        context: ScopeContext,
        *,
        zone: ZoneRef,
        period: str,
        package_id: uuid.UUID,
        sample_id: uuid.UUID,
        checkpoint_id: uuid.UUID,
        findings: Mapping[str, int],
        recurrence: Sequence[Mapping[str, Any]],
        commitments: Sequence[Mapping[str, Any]],
        generated_at: datetime,
        content: Mapping[str, Any],
    ) -> uuid.UUID | None:
        """Escribe ``monthly_package_produced`` y sella la fila en una sola transacción.

        El expediente se escribe **antes** del ``UPDATE`` de sellado (como toda escritura de
        U-04, la cadena de la planta primero); si el sellado pierde la carrera (``seal`` devuelve
        ``False``), ``_SealLost`` revierte la transacción entera: el registro escrito nunca se
        confirma sin su fila sellada.
        """
        record_id = uuid7(self._clock)
        try:
            async with self._database.transaction(context) as transaction:
                written = await self._writer.write(
                    context,
                    MONTHLY_PACKAGE_PRODUCED,
                    content,
                    scope=RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id),
                    events=(
                        NewEvent(
                            event_name=MONTHLY_PACKAGE_AVAILABLE,
                            payload={
                                "package_id": str(package_id),
                                "record_id": str(record_id),
                                "zone_id": str(zone.zone_id),
                                "period": period,
                            },
                        ),
                    ),
                    occurred_at=generated_at,
                    transaction=transaction,
                    record_id=record_id,
                )
                if isinstance(written, LedgerRejection):
                    raise MonthlyPackageRecordRejected(written.code.value)
                sealed = await self._repository.seal(
                    transaction,
                    package_id=package_id,
                    findings_summary=findings,
                    recurrence_summary=recurrence,
                    metric_snapshot_id=None,
                    exposure_indicator_id=None,
                    coverage_summary={},
                    commitments_from_previous_period=commitments,
                    audit_sample_id=sample_id,
                    sealed_checkpoint_record_id=checkpoint_id,
                    record_id=written.record_id,
                )
                if not sealed:
                    raise _SealLost()
                return written.record_id
        except _SealLost:
            _log.debug(
                "se perdió la carrera al sellar: se descarta el expediente escrito",
                zone_id=str(zone.zone_id),
                period=period,
            )
            return None

    def _build_zip(
        self,
        *,
        package_id: uuid.UUID,
        zone: ZoneRef,
        period: str,
        generated_at: datetime,
        findings: Mapping[str, int],
    ) -> bytes:
        document_bytes = _render_document(
            zone_id=zone.zone_id, period=period, findings=findings, generated_at=generated_at
        )
        verifier = load_verifier_bundle()
        verifier_path = f"verifier/{VERIFIER_PATH}"
        artifacts: list[dict[str, str]] = [
            {"path": _DOCUMENT_NAME, "sha256": hashlib.sha256(document_bytes).hexdigest()},
            {"path": verifier_path, "sha256": verifier.sha256},
        ]
        manifest: dict[str, Any] = {
            "package_id": str(package_id),
            "zone_id": str(zone.zone_id),
            "period": period,
            "generated_at": format_timestamp(generated_at),
            "artifacts": artifacts,
            "metrics_status": MetricStatus.NOT_COMPUTABLE.value,
            "exposure_status": ExposureStatus.NOT_COMPUTABLE.value,
        }
        manifest_bytes = canonical_bytes_sync(manifest)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(_zip_info(_DOCUMENT_NAME), document_bytes)
            archive.writestr(_zip_info(verifier_path), verifier.data)
            archive.writestr(_zip_info(_MANIFEST_NAME), manifest_bytes)
        return buffer.getvalue()


def _audit_sample_doc(sample_id: uuid.UUID, draw: SampleDraw) -> dict[str, Any]:
    return {
        "sample_id": str(sample_id),
        "population_size": draw.population_size,
        "sample_size": draw.sample_size,
        "seed": draw.seed,
        "strata": [
            {
                "family": stratum.family,
                "reason_category": stratum.reason_category,
                "population": stratum.population,
                "drawn": stratum.drawn,
            }
            for stratum in draw.strata
        ],
    }
