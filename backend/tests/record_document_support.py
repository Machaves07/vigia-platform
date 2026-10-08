"""Actas sintéticas para las pruebas y el banco del documento legible (TASK-217, NFR-GOB-05).

``record_body`` construye el cuerpo de ``GET /commissioning-records/{id}`` validado por
``CommissioningRecordOut`` (la misma forma que la ruta), con ``standards`` estándares (4 filas por
estándar, una por postura), ``cameras`` cámaras y ``passes`` pases por celda. La **matriz máxima**
de NFR-GOB-05 es ``record_body(MAX_STANDARDS, MAX_CAMERAS, 3)``: 128 filas con 3 evidencias sin
clip por fila (el peor caso del tamaño), 8 cámaras, los cuatro tramos medidos, la aceptación de
falsas alarmas y una firma por rol.

``html_fields`` lee del HTML intermedio cada ``data-field`` con su texto, y ``json_leaves`` aplana
la respuesta JSON con las mismas rutas: la prueba campo a campo compara las dos.

Solo datos generados (NFR-CTR-43): identificadores derivados de una semilla, sin personas.
"""

from __future__ import annotations

import json
import uuid
from html.parser import HTMLParser
from typing import Any, Final

from vigia_platform.catalog.adapters.http.commissioning_records import CommissioningRecordOut
from vigia_platform.catalog.domain.enums import OcclusionVerification, StepKind
from vigia_platform.catalog.domain.scope_record import MAX_CAMERAS
from vigia_platform.catalog.domain.standard import MAX_STANDARDS
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH

__all__ = [
    "HOSTILE_REASON",
    "MAX_CAMERAS",
    "MAX_PASSES",
    "MAX_STANDARDS",
    "assert_field_by_field",
    "expected_text",
    "html_fields",
    "json_leaves",
    "record_body",
]

MAX_PASSES: Final = 3
"""Pases por celda de la matriz máxima de NFR-GOB-05."""
HOSTILE_REASON: Final = (
    'Reflejo del portón <b>& "brillo"</b> <script>alert(1)</script> <img src="http://e/x.png">'
    " &amp; 'fin'\N{NO-BREAK SPACE}ñ"
)
"""Un ``reason_es`` que, sin escapar, sería marcado, script y una petición de red."""
_NAMESPACE: Final = uuid.UUID("0b8f8a51-7d0e-4c55-9a0e-5f5e0a7d2c17")
_ROLES: Final = ("coordinator_sst", "plant_manager", "line_manager", "copasst")


def _id(*parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, "/".join(str(part) for part in parts)))


def _tranche(median: int, measured_by: str, repetitions: int) -> dict[str, Any]:
    return {
        "median_ms": median,
        "p95_ms": median * 2,
        "max_ms": median * 3,
        "repetitions": repetitions,
        "measured_by": measured_by,
    }


def record_body(
    standards: int = 2,
    cameras: int = 2,
    passes: int = MAX_PASSES,
    *,
    seed: str = "acta",
    reason_es: str | None = "Las falsas alarmas se deben al reflejo del portón",
    measured: bool = True,
) -> dict[str, Any]:
    """El cuerpo JSON de ``GET /commissioning-records/{id}`` de un acta sintética."""
    zone = _id(seed, "zone")
    camera_ids = [_id(seed, "camera", index) for index in range(cameras)]
    rows = standards * 4
    matrix: list[dict[str, Any]] = [
        {
            "row_id": _id(seed, "row", row),
            "detected": passes - (row % 2),
            "missed": 0,
            "false_alarms": row % 2,
            "unverifiable_evidence_refs": [_id(seed, "evidence", row, n) for n in range(passes)]
            if row % 3 != 2
            else [],
        }
        for row in range(rows)
    ]
    durations = [60_000 * (index + 1) for index in range(len(StepKind))]
    steps = [
        {"step_kind": kind.value, "duration_ms": duration}
        for kind, duration in zip(StepKind, durations, strict=True)
    ]
    tranches: dict[str, Any] = {
        "node_tranche": _tranche(150, "installer", 1),
        "platform_tranche": _tranche(400, "platform", 100) if measured else None,
        "exposure_tranche": _tranche(120, "browser", 100) if measured else None,
        "served_tranche": _tranche(150, "platform", 100),
    }
    body: dict[str, Any] = {
        "commissioning_record_id": _id(seed, "record"),
        "session_id": _id(seed, "session"),
        "zone_id": zone,
        "catalog_version": 7,
        "kind": "initial",
        "passes_per_cell": passes,
        "matrix_results": matrix,
        "false_negatives_total": 0,
        "false_alarm_rate_observed": round(
            sum(r["false_alarms"] for r in matrix) / max(1, rows * passes), 6
        ),
        "false_alarm_threshold": 0.0,
        "false_alarm_acceptance": None
        if reason_es is None
        else {
            "reason_es": reason_es,
            "accepted_by": _id(seed, "signer", 0),
            "accepted_at": "2026-10-07T10:15:00.000Z",
        },
        "latency": {
            **tranches,
            "not_measured": [] if measured else ["platform_tranche", "exposure_tranche"],
            "indicative_sum_median_ms": 820 if measured else None,
            "indicative_sum_p95_ms": 1640,
            "indicative": True,
            "repetitions_counted": 100 if measured else None,
        },
        "cameras_measured": [
            {
                "camera_id": camera,
                "measured_fps": 14.5 + index if index % 4 else None,
                "declared_min_fps": 10.0 if index % 2 else None,
            }
            for index, camera in enumerate(camera_ids)
        ],
        "occlusion_summary": [
            {
                "camera_id": camera,
                "verification": list(OcclusionVerification)[index % 3].value,
                "test_id": _id(seed, "occlusion", index) if index % 2 == 0 else None,
            }
            for index, camera in enumerate(camera_ids)
        ],
        "steps_summary": steps,
        "installer_measurements": {
            "beacon_latency_ms_p95": 180,
            "measured_by": "installer",
            "baselines": [
                {"camera_id": camera, "zone_id": zone, "captured_at": "2026-10-01T08:00:00.000Z"}
                for camera in camera_ids
            ],
        },
        "signatures": [
            {
                "user_id": _id(seed, "signer", index),
                "role_in_use": role,
                "signed_at": f"2026-10-07T10:2{index}:00.000Z",
            }
            for index, role in enumerate(_ROLES)
        ],
        "closed_at": "2026-10-07T10:30:00.000Z",
    }
    body["total_duration_ms"] = sum(durations)
    view: dict[str, Any] = CommissioningRecordOut.model_validate_json(json.dumps(body)).model_dump(
        mode="json"
    )
    return view


_LABELS: Final = json.loads(DEFAULT_LABELS_PATH.read_text(encoding="utf-8"))
_ENUMERATIONS: Final = {
    "kind": "walk_test_kind",
    "step_kind": "step_kind",
    "verification": "occlusion_verification",
    "role_in_use": "role",
}
_CLOCKS: Final = {
    "installer": "Instalador",
    "platform": "Plataforma",
    "browser": "Navegador",
    "node": "Nodo",
}
_TRANCHES: Final = {
    "node_tranche": "Tramo 1: del hecho al aviso del nodo",
    "platform_tranche": "Tramo 2: de la concesión a la recepción del clip",
    "exposure_tranche": "Tramo 3a: exposición en la consola",
    "served_tranche": "Tramo 3b: del clip recibido al primer servicio",
}


def expected_text(path: str, value: Any) -> str:
    """El texto que el documento debe mostrar para la hoja ``path``, sin el código del render:
    etiqueta de ``labels.platform.es.json`` o rótulo del documento, ``—`` para nulo, ``Sí`` para
    verdadero, decimal con coma, y el resto tal cual."""
    last = path.rsplit(".", 1)[-1]
    if value is None:
        return "—"
    if last in _ENUMERATIONS:
        text: str = _LABELS[_ENUMERATIONS[last]][value]
        return text
    if last == "measured_by":
        return _CLOCKS[value]
    if path.startswith("latency.not_measured."):
        return _TRANCHES[value]
    if value is True:
        return "Sí"
    if value is False:
        return "No"
    if isinstance(value, float):
        return repr(value).replace(".", ",")
    return str(value)


def assert_field_by_field(record: dict[str, Any], html: str) -> None:
    """Cada hoja de ``record`` está en ``html``, con su texto, y no hay ninguna más."""
    leaves = json_leaves(record)
    shown = html_fields(html)
    assert set(shown) == set(leaves), (
        sorted(set(leaves) - set(shown)),
        sorted(set(shown) - set(leaves)),
    )
    wrong = {
        path: (shown[path], expected_text(path, value))
        for path, value in leaves.items()
        if shown[path] != expected_text(path, value)
    }
    assert not wrong, wrong


def json_leaves(value: Any, path: str = "") -> dict[str, Any]:
    """Cada hoja de la respuesta JSON con su ruta (``matrix_results.3.detected``)."""
    if isinstance(value, dict):
        found: dict[str, Any] = {}
        for key, item in value.items():
            found.update(json_leaves(item, f"{path}.{key}" if path else key))
        return found
    if isinstance(value, list):
        found = {}
        for index, item in enumerate(value):
            found.update(json_leaves(item, f"{path}.{index}"))
        return found
    return {path: value}


class _Fields(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.repeated: list[str] = []
        self._open: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "meta"):
            return
        field = dict(attrs).get("data-field")
        self._open.append((tag, field))
        if field is not None:
            if field in self.fields:
                self.repeated.append(field)
            self.fields[field] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("br", "meta"):
            return
        while self._open:
            opened, _ = self._open.pop()
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        for _, field in self._open:
            if field is not None:
                self.fields[field] += data


def html_fields(html: str) -> dict[str, str]:
    """``data-field`` → texto (sin escapar) del HTML intermedio; falla si una ruta se repite."""
    parser = _Fields()
    parser.feed(html)
    parser.close()
    assert not parser.repeated, f"rutas repetidas en el documento: {parser.repeated}"
    return parser.fields
