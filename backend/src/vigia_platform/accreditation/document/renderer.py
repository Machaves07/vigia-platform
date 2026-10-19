"""``accreditation.document`` (LC-LAZ-23; NFR-LAZ-18, 27, 54; PAT-LAZ-SEG-02, REN-03; TASK-319).

El documento portátil legible que un inspector o un juez abre primero. Composición
**determinista**: con el mismo ``ManifestInputs`` (el mismo alcance exportado), el mismo
``document_sha256``, en cualquier proceso y con cualquier zona horaria del proceso.

- **Determinismo sin reloj**: el identificador del PDF (``/ID``) y sus fechas (``/CreationDate``,
  ``/ModDate``) nunca salen de ``datetime.now()``. El identificador sale del hash del HTML ya
  renderizado (el contenido, no un UUID al azar); las fechas salen de ``inputs.generated_at``, que
  el compositor fija en el primer intento de la exportación (TASK-345, PR-LAZ-49) y no cambia entre
  reintentos. WeasyPrint solo escribe ``/CreationDate``/``/ModDate`` cuando el HTML declara
  ``<meta name="dcterms.created">``; sin esa marca no escribe ninguna (nunca la hora real).
- **Fuente**: Noto Sans ya empaquetada (``backend/resources/fonts/noto-sans``, SIL OFL 1.1, A-53),
  con cobertura completa del español; sin fuente nueva. El mismo patrón de
  ``catalog.adapters.rendering.record_document`` (TASK-217): un ``PackagedFontFetcher`` que solo
  sirve esos dos archivos y detiene el render ante cualquier otra URL (red, ``data:``, otra ruta).
- **Sin contenido activo**: la plantilla no tiene ``<script>``, ``<form>`` ni enlaces; WeasyPrint no
  ejecuta JavaScript y el único ``url_fetcher`` posible son las fuentes empaquetadas (NFR-LAZ-18).
- **Ninguna enumeración cruda**: todo valor cerrado se muestra por ``labels.loop.es.json`` /
  ``labels.platform.es.json`` (``PlatformLabels``); un valor sin etiqueta detiene la generación
  (fallo cerrado, como en TASK-217).

**La organización en siete secciones (``DOCUMENT_SECTIONS``)** sigue los ejes con los que la
Resolución 0312 de 2019 estructura sus estándares mínimos para el capítulo de más de 50
trabajadores (artículo 16): Recursos, Gestión Integral del SG-SST, Gestión de la Salud, Gestión
de Peligros y Riesgos, Gestión de Amenazas, Verificación y Mejoramiento. El diseño (LC-LAZ-23,
NFR-LAZ-54) exige organizar el contenido según esos soportes pero no desglosa una lista de
secciones; ``aidlc-docs`` tampoco la trae. Esta es la interpretación de esta tarea, adaptada al
contenido que Vigía certifica (la zona, nunca la persona, P3): cada eje recibe la parte del
alcance exportado que mejor le corresponde (BR-LAZ-90). Queda declarada aquí, en código, para que
una revisión del dueño la pueda corregir sin tocar el resto del módulo.

Módulo puro en sus datos (``document_content``, ``document_html``): no lee la hora del sistema
(``generate_document`` recibe ``ManifestInputs``, ya con ``generated_at`` fijado). El render de
WeasyPrint sí usa E/S de archivos (las fuentes empaquetadas); por eso ``ExportComposer`` lo corre
en un hilo aparte (``asyncio.to_thread``), para no bloquear el bucle de eventos.
"""

from __future__ import annotations

import functools
import hashlib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import jinja2
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration
from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetcherResponse

from vigia_platform.accreditation.manifest.model import (
    AGGREGATION_NOTICE_ES,
    ANONYMIZATION_STATEMENT_ES,
    COVERAGE_NOTICE_ES,
    EVIDENCE_FLAGGED_NOTICE_ES,
    REGRESSION_NOTICE_ES,
    REVOCATION_NOTICE_ES,
    VERIFIER_ABSENT_ES,
    WITH_CLIPS_NOTICE_ES,
    WITHOUT_CLIPS_NOTICE_ES,
    ManifestInputs,
    ZoneEntry,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState, PackageForm
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "DOCUMENT_SECTIONS",
    "FONT_DIRECTORY",
    "FONT_FILES",
    "NULL_TEXT",
    "DocumentRenderFailed",
    "PackagedFontFetcher",
    "document_content",
    "document_html",
    "generate_document",
    "render_pdf",
]

DOCUMENT_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("I", "Recursos"),
    ("II", "Gestión integral del sistema de gestión de la seguridad y salud en el trabajo"),
    ("III", "Gestión de la salud"),
    ("IV", "Gestión de peligros y riesgos"),
    ("V", "Gestión de amenazas"),
    ("VI", "Verificación"),
    ("VII", "Mejoramiento"),
)
"""Los soportes de la Resolución 0312 de 2019, artículo 16, en el orden que organiza el
documento. Lista **cerrada**: ``test_export_document_sections.py`` comprueba, por igualdad
estructural, que las siete aparecen en el HTML (``data-section``)."""

RENDERING: Final = Path(__file__).resolve().parent
TEMPLATES: Final = RENDERING / "templates"
STYLESHEET: Final = TEMPLATES / "export_document.css"
FONT_DIRECTORY: Final = Path(__file__).resolve().parents[4] / "resources" / "fonts" / "noto-sans"
"""``backend/resources/fonts/noto-sans`` (la imagen la copia con ``backend/resources``)."""
FONT_FILES: Final = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")
"""Los únicos recursos que un render puede pedir (los cita ``export_document.css``)."""

NULL_TEXT: Final = "—"
"""Lo que muestra el documento para un valor nulo."""


class DocumentRenderFailed(Exception):
    """El render pidió un recurso que no es una fuente empaquetada (error del código)."""


class _ResourceDenied(FatalURLFetchingError):
    """Detiene WeasyPrint: ``FatalURLFetchingError`` es lo único que no convierte en aviso."""


@functools.cache
def _environment() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
    )


@functools.cache
def _font_bytes(path: Path) -> bytes:
    return path.read_bytes()


class PackagedFontFetcher(URLFetcher):
    """``url_fetcher`` de un render: registra cada URL y solo sirve las fuentes empaquetadas.

    El mismo patrón que ``catalog.adapters.rendering.record_document.PackagedFontFetcher``
    (TASK-217): solo una URL ``file:`` sin máquina, consulta ni fragmento, cuya ruta sea
    **exactamente** uno de ``FONT_FILES`` en ``FONT_DIRECTORY``. Todo lo demás deniega con un
    error fatal que detiene el render.
    """

    def __init__(
        self, directory: Path = FONT_DIRECTORY, files: tuple[str, ...] = FONT_FILES
    ) -> None:
        super().__init__(allowed_protocols=frozenset({"file"}), allow_redirects=False)
        self._allowed = {str(directory / name): directory / name for name in files}
        self.requested: list[str] = []
        """Cada URL pedida durante el render, en orden (también las denegadas)."""

    def _packaged(self, url: str) -> Path | None:
        parts = urlsplit(url)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            return None
        if parts.query or parts.fragment or "?" in url or "#" in url:
            return None
        return self._allowed.get(url2pathname(unquote(parts.path)))

    def fetch(self, url: str, headers: Mapping[str, str] | None = None) -> URLFetcherResponse:
        self.requested.append(url)
        path = self._packaged(url)
        if path is None:
            raise _ResourceDenied("recurso denegado: solo se sirven las fuentes empaquetadas")
        return URLFetcherResponse(url, _font_bytes(path), {"Content-Type": "font/ttf"})


def render_pdf(
    html: str, *, document_id: bytes, fetcher: PackagedFontFetcher | None = None
) -> bytes:
    """El PDF de ``html``, con ``/ID`` fijado a ``document_id`` (derivado del contenido).

    ``/CreationDate`` y ``/ModDate`` no se fijan aquí: salen de ``<meta name="dcterms.*">`` en
    ``html``, que ``document_html`` ya escribió a partir de ``generated_at``.

    ``full_fonts=True``: WeasyPrint subconjunta (subset) las fuentes por defecto, y el orden de
    los glifos de un subconjunto depende de tablas hash de Pango/HarfBuzz (C, no de Python) que
    varían entre procesos aunque ``PYTHONHASHSEED`` esté fijo; eso cambia la compresión del flujo
    de la fuente y, con ella, ``document_sha256`` de una generación a otra (medido: sin
    ``full_fonts``, dos llamadas en el mismo proceso coinciden, pero dos procesos nunca). Con
    ``full_fonts=True`` se empaquetan los dos archivos ``.ttf`` completos y sin tocar: mismos
    bytes siempre, a cambio de un documento más grande.
    """
    fetcher = fetcher if fetcher is not None else PackagedFontFetcher()
    base_url = str(FONT_DIRECTORY) + "/"
    fonts = FontConfiguration()
    try:
        stylesheet = CSS(
            string=STYLESHEET.read_text(encoding="utf-8"),
            base_url=base_url,
            url_fetcher=fetcher,
            font_config=fonts,
        )
        document = HTML(string=html, base_url=base_url, url_fetcher=fetcher)
        return document.write_pdf(
            stylesheets=[stylesheet],
            font_config=fonts,
            pdf_identifier=document_id,
            full_fonts=True,
        )
    except FatalURLFetchingError:
        raise DocumentRenderFailed("el documento pidió un recurso no empaquetado") from None


# --- Contenido -------------------------------------------------------------------------------


def _label(labels: PlatformLabels, enumeration: str, value: object) -> str:
    return labels.label(enumeration, value)


def _optional_label(labels: PlatformLabels, enumeration: str, value: str | None) -> str:
    return NULL_TEXT if value is None else _label(labels, enumeration, value)


def _by_state(labels: PlatformLabels, counts: Mapping[str, int]) -> list[dict[str, Any]]:
    return [
        {
            "label": _label(labels, "finding_lifecycle_state", state.value),
            "count": counts[state.value],
        }
        for state in FindingLifecycleState
    ]


def _zone_ref(zone: ZoneEntry) -> dict[str, str]:
    return {"zone_id": str(zone.zone_id), "code": zone.code, "name": zone.name}


def document_content(inputs: ManifestInputs, labels: PlatformLabels) -> dict[str, Any]:
    """El contexto de la plantilla: todo valor de lista cerrada ya pasado por su etiqueta.

    Puro: ninguna llamada aquí lee E/S ni la hora del sistema; ``inputs.generated_at`` ya viene
    fijado por el compositor (TASK-345).
    """
    zones_by_id = {zone.zone_id: zone for zone in inputs.zones}
    form = PackageForm(inputs.package_form)
    package_form_notice_es = (
        WITHOUT_CLIPS_NOTICE_ES if form is PackageForm.WITHOUT_CLIPS else WITH_CLIPS_NOTICE_ES
    )

    findings_by_zone: dict[Any, Counter[str]] = {zone_id: Counter() for zone_id in zones_by_id}
    evidence_flagged_any = False
    for finding in inputs.findings:
        findings_by_zone[finding.zone_id][finding.lifecycle_state] += 1
        evidence_flagged_any = evidence_flagged_any or finding.evidence_flagged
    total_by_state: Counter[str] = Counter()
    for counter in findings_by_zone.values():
        total_by_state.update(counter)

    zones_sorted = sorted(inputs.zones, key=lambda zone: zone.code)
    aggregated = any(zone.single_occupancy for zone in zones_sorted)

    coverage_by_zone = sorted(inputs.coverage, key=lambda item: zones_by_id[item.zone_id].code)
    standards_sorted = sorted(
        inputs.standards_cited,
        key=lambda item: (zones_by_id[item.zone_id].code, item.title_es, item.version),
    )
    regressions_sorted = sorted(
        inputs.regression_periods, key=lambda item: (zones_by_id[item.zone_id].code, item.starts_at)
    )
    revoked_sorted = sorted(
        inputs.revoked_gates, key=lambda item: (zones_by_id[item.zone_id].code, item.since)
    )
    agreement_zones = [zone for zone in zones_sorted if zone.agreements]
    checkpoints_sorted = sorted(
        inputs.checkpoints, key=lambda item: (item.kind, str(item.plant_id or ""))
    )
    keys_sorted = sorted(inputs.checkpoint_public_keys, key=lambda key: key["key_id"])

    return {
        "export_id": str(inputs.export_id),
        "organization": {"code": inputs.organization.code, "name": inputs.organization.name},
        "plant": {
            "code": inputs.plant.code,
            "name": inputs.plant.name,
            "country": inputs.plant.country,
            "timezone": inputs.plant.timezone,
        },
        "period_from": format_timestamp(inputs.period_from),
        "period_to": format_timestamp(inputs.period_to),
        "generated_at": format_timestamp(inputs.generated_at),
        "generated_by_name": inputs.generated_by.display_name or NULL_TEXT,
        "generated_by_role": _optional_label(labels, "role", inputs.generated_by.role_in_use),
        "package_form_label": _label(labels, "package_form", form.value),
        "package_form_notice_es": package_form_notice_es,
        "zones": [_zone_ref(zone) for zone in zones_sorted],
        "counts": {
            "finding_count": len(inputs.findings),
            "evidence_flagged_count": sum(1 for f in inputs.findings if f.evidence_flagged),
            "by_state": _by_state(labels, total_by_state),
        },
        "zone_findings": [
            {
                "zone": _zone_ref(zone),
                "single_occupancy": zone.single_occupancy,
                "by_state": _by_state(labels, findings_by_zone[zone.zone_id]),
            }
            for zone in zones_sorted
        ],
        "aggregation_notice_es": AGGREGATION_NOTICE_ES if aggregated else None,
        "evidence_flagged_notice_es": EVIDENCE_FLAGGED_NOTICE_ES if evidence_flagged_any else None,
        "coverage_notice_es": COVERAGE_NOTICE_ES,
        "zone_coverage": [
            {
                "zone": _zone_ref(zones_by_id[item.zone_id]),
                "observable_ms": item.observable_ms,
                "degraded_ms": item.degraded_ms,
                "not_observable_ms": item.not_observable_ms,
                "no_communication_ms": item.no_communication_ms,
                "never_reported_ms": item.never_reported_ms,
                "zone_not_active_ms": item.zone_not_active_ms,
                "not_elapsed_ms": item.not_elapsed_ms,
                "total_ms": item.total_ms,
            }
            for item in coverage_by_zone
        ],
        "standards": [
            {
                "zone": _zone_ref(zones_by_id[item.zone_id]),
                "title_es": item.title_es,
                "version": item.version,
                "declared_text_sha256": item.declared_text_sha256,
            }
            for item in standards_sorted
        ],
        "agreements": [
            {
                "zone": _zone_ref(zone),
                "agreements": [
                    {
                        "status_label": _label(labels, "agreement_status", agreement.status),
                        "approved_at": format_timestamp(agreement.approved_at),
                        "signatories": [
                            {
                                "role_label": _label(labels, "role", signatory.role),
                                "confirmed_at": (
                                    NULL_TEXT
                                    if signatory.confirmed_at is None
                                    else format_timestamp(signatory.confirmed_at)
                                ),
                            }
                            for signatory in agreement.signatories
                        ],
                    }
                    for agreement in sorted(zone.agreements, key=lambda a: a.approved_at)
                ],
            }
            for zone in agreement_zones
        ],
        "regression_notice_es": REGRESSION_NOTICE_ES if regressions_sorted else None,
        "regression_periods": [
            {
                "zone": _zone_ref(zones_by_id[item.zone_id]),
                "starts_at": format_timestamp(item.starts_at),
                "ends_at": NULL_TEXT if item.ends_at is None else format_timestamp(item.ends_at),
                "cause": _label(labels, "regression_cause", item.cause),
            }
            for item in regressions_sorted
        ],
        "revocation_notice_es": REVOCATION_NOTICE_ES if revoked_sorted else None,
        "revoked_gates": [
            {
                "zone": _zone_ref(zones_by_id[item.zone_id]),
                "since": format_timestamp(item.since),
                "until": NULL_TEXT if item.until is None else format_timestamp(item.until),
                "reason_es": item.reason_es or NULL_TEXT,
            }
            for item in revoked_sorted
        ],
        "checkpoints": [
            {
                "kind_label": _label(labels, "checkpoint_kind", item.kind),
                "sequence": item.sequence,
                "entry_hash": item.entry_hash,
            }
            for item in checkpoints_sorted
        ],
        "public_keys": [
            {
                "key_id": key["key_id"],
                "status_label": _label(labels, "key_status", key["status"]),
                "valid_from": key["valid_from"],
                "valid_until": key["valid_until"],
            }
            for key in keys_sorted
        ],
        "anonymization_statement_es": ANONYMIZATION_STATEMENT_ES,
        "verifier_absent_es": VERIFIER_ABSENT_ES,
        "sections": DOCUMENT_SECTIONS,
    }


def document_html(inputs: ManifestInputs, labels: PlatformLabels | None = None) -> str:
    """El HTML intermedio del documento, con las siete secciones (``DOCUMENT_SECTIONS``)."""
    labels = labels if labels is not None else PlatformLabels.load()
    template = _environment().get_template("export_document.html")
    return template.render(**document_content(inputs, labels))


def generate_document(inputs: ManifestInputs, labels: PlatformLabels | None = None) -> bytes:
    """El PDF de ``inputs`` (``generar`` de LC-LAZ-23): mismo ``inputs``, mismos bytes.

    El identificador del PDF sale del hash del HTML ya renderizado: dos llamadas con el mismo
    ``inputs`` producen el mismo HTML y, por tanto, el mismo identificador y los mismos bytes.
    """
    html = document_html(inputs, labels)
    document_id = hashlib.sha256(html.encode("utf-8")).digest()[:16]
    return render_pdf(html, document_id=document_id)
