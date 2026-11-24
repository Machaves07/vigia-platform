"""Lectura de los valores de proceso y del nombre de cada señal (TASK-595; A-129; BR-LAZ-06).

Sin base, con la composición pura del detalle (``finding_detail`` + ``detail_view``), de la ficha
de vida (``finding_timeline`` + ``timeline_view``) y de la foto del episodio (``_view`` de SCR-06),
y con ``FindingDetailService.record_declarations`` sobre un puerto de catálogo de prueba:

- **Detalle con valores**: un tramo con 2 analógicas y 2 binarias devuelve nombre, unidad,
  decimales, par de estados, valor en el instante del hecho, mínimo, máximo, último, serie y
  cambios tal como los guardó la ingesta (sin recalcular ni redondear).
- **Zona unipersonal**: ``at_event.read_at`` llega agregado (``time_aggregated``) y los
  desplazamientos no cambian (mutación ``m_hora_exacta_en_unipersonal``).
- **Sin declaración**: una variable que ya no está en ninguna versión deja los campos resueltos en
  ``null`` y el detalle se compone (mutación ``m_error_sin_declaracion``).
- **Señales por nombre**: ``code`` y ``description_es`` de la versión del registro, no de la
  vigente hoy, aunque una versión posterior renombre la señal (mutación ``m_catalogo_vigente``).
- **Ficha y foto**: el hito de detección trae el resumen sin la serie; la foto con bloque lo
  devuelve y sin bloque, ``null``.
- **Una consulta, sin auditoría**: ``record_declarations`` pide las versiones una vez para todos los
  registros y, si el puerto no responde, los nombres van ``null`` sin error.

Solo datos sintéticos.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from vigia_contracts.models.enumerations import GateStatus

from tests.writer_support import unit_context
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.fleet.adapters.http.episode_photos import _view as photo_out
from vigia_platform.fleet.adapters.postgres.episode_photo_queries import EpisodePhotoSummary
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.record_declarations import (
    DeclaredCatalogVersion,
    photo_version,
    record_version,
)
from vigia_platform.ledger.application.reader import LedgerRecordView, RecordActor
from vigia_platform.loop.adapters.http.findings_routes import detail_view
from vigia_platform.loop.adapters.http.timeline_routes import timeline_view
from vigia_platform.loop.application.detail import (
    DetailInputs,
    FindingDetailService,
    RecordFacts,
    finding_detail,
)
from vigia_platform.loop.application.timeline import (
    TimelineInputs,
    TimelineSource,
    finding_timeline,
)
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.signing.keys import format_timestamp

ORGANIZATION: Final = uuid.uuid4()
PLANT: Final = uuid.uuid4()
ZONE: Final = uuid.uuid4()
STANDARD: Final = uuid.uuid4()
CAMERA: Final = uuid.uuid4()
SPEED: Final = uuid.uuid4()
METERS: Final = uuid.uuid4()
JUMBO: Final = uuid.uuid4()
CRANE: Final = uuid.uuid4()
ENERGY_SIGNAL: Final = uuid.uuid4()
JUMBO_SIGNAL: Final = uuid.uuid4()
CRANE_SIGNAL: Final = uuid.uuid4()

ISSUED: Final = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
STARTED: Final = datetime(2026, 11, 20, 6, 20, 10, 250000, tzinfo=UTC)
RECEIVED: Final = STARTED + timedelta(seconds=45)
WINDOW_MINUTES: Final = 60


def _signals(jumbo_name: str = "Jumbo en posición") -> list[dict[str, Any]]:
    return [
        {"signal_id": str(ENERGY_SIGNAL), "code": "SIG-ENE", "role": "energy",
         "description_es": "Energía de la máquina"},
        {"signal_id": str(JUMBO_SIGNAL), "code": "DI5", "role": "auxiliary",
         "description_es": jumbo_name},
        {"signal_id": str(CRANE_SIGNAL), "code": "DI6", "role": "auxiliary",
         "description_es": "Puente grúa en reposo"},
    ]  # fmt: skip


def _variables(*, with_crane: bool = True) -> list[dict[str, Any]]:
    variables: list[dict[str, Any]] = [
        {"variable_id": str(SPEED), "code": "VEL-MAQ", "kind": "analog",
         "description_es": "Velocidad de la máquina", "unit": "m_per_min", "decimals": 0},
        {"variable_id": str(METERS), "code": "MET-JUMBO", "kind": "analog",
         "description_es": "Metros del jumbo", "unit": "m", "decimals": 1},
        {"variable_id": str(JUMBO), "code": "JUMBO-POS", "kind": "binary",
         "description_es": "Jumbo en posición", "signal_id": str(JUMBO_SIGNAL),
         "states": "in_position_out"},
    ]  # fmt: skip
    if with_crane:
        variables.append(
            {
                "variable_id": str(CRANE),
                "code": "GRUA",
                "kind": "binary",
                "description_es": "Puente grúa",
                "signal_id": str(CRANE_SIGNAL),
                "states": "at_rest_moving",
            }
        )
    return variables


def _payload(
    *, jumbo_name: str = "Jumbo en posición", variables: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "standards": [{"standard_id": str(STANDARD), "version": 1}],
        "signals": _signals(jumbo_name),
        "thresholds": {"review": 0.5, "publication": 0.8},
        "cameras": [{"camera_id": str(CAMERA)}],
        "process_context": {
            "before_ms": 30_000,
            "variables": _variables() if variables is None else variables,
        },
    }


def _version(
    version: int,
    issued: datetime,
    superseded: datetime | None = None,
    **changes: Any,
) -> DeclaredCatalogVersion:
    return DeclaredCatalogVersion.from_payload(version, issued, superseded, _payload(**changes))


BLOCK: Final[dict[str, Any]] = {
    "from_offset_ms": -30_000,
    "to_offset_ms": 12_000,
    "variables": [
        {
            "variable_id": str(SPEED),
            "kind": "analog",
            "at_event": {"value": 1350.25, "valid": True, "offset_ms": 0},
            "min": 1349.5,
            "max": 1401.75,
            "last": 1351,  # el JSON canónico escribe 1351.0 como 1351: sigue siendo 1351
            "samples": [
                {"offset_ms": -30_000, "value": 1401.75},
                {"offset_ms": -10_000},
                {"offset_ms": 0, "value": 1350.25},
            ],
        },
        {
            "variable_id": str(METERS),
            "kind": "analog",
            "at_event": {"valid": False, "offset_ms": 1_500},
            "samples": [],
        },
        {
            "variable_id": str(JUMBO),
            "kind": "binary",
            "at_event": {"value": "asserted", "valid": True, "offset_ms": 0},
            "changes": [
                {"offset_ms": -30_000, "value": "deasserted"},
                {"offset_ms": -4_000, "value": "asserted"},
            ],
            "truncated": False,
        },
        {
            "variable_id": str(CRANE),
            "kind": "binary",
            "at_event": {"value": "unknown", "valid": False, "offset_ms": 0},
            "changes": [{"offset_ms": -30_000, "value": "unknown"}],
            "truncated": True,
        },
    ],
}


def _document(block: Any = BLOCK) -> dict[str, Any]:
    document: dict[str, Any] = {
        "finding_id": str(uuid.uuid4()),
        "episode": {"episode_id": str(uuid.uuid4()), "segment_index": 0},
        "standard": {"standard_id": str(STANDARD), "version": 1},
        "node_time": {
            "started_at": STARTED.isoformat(),
            "ended_at": (STARTED + timedelta(seconds=12)).isoformat(),
            "clock": {"synchronized": True, "offset_ms": 40},
        },
        "signals": [
            {
                "signal_id": str(sid),
                "role": role,
                "value": "asserted",
                "valid": True,
                "read_at": STARTED.isoformat(),
            }
            for sid, role in (
                (ENERGY_SIGNAL, "energy"),
                (JUMBO_SIGNAL, "auxiliary"),
                (CRANE_SIGNAL, "auxiliary"),
            )
        ],
        "max_confidence": 0.9,
        "automatic_classification": {"trigger": "publication_threshold"},
    }
    if block is not None:
        document["process_context"] = block
    return document


def _ledger(document: dict[str, Any], record_id: uuid.UUID | None = None) -> LedgerRecordView:
    return LedgerRecordView(
        record_id=record_id or uuid.uuid4(),
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        chain_sequence=7,
        record_type="finding_received",
        schema_version=1,
        actor=RecordActor("node", uuid.uuid4(), "Nodo", None, None, "u03"),
        scope_plant_id=PLANT,
        scope_zone_id=ZONE,
        scope_node_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        received_at=RECEIVED,
        occurred_at=STARTED,
        source_key=None,
        content=json.dumps(document).encode(),
        content_hash="0" * 64,
        previous_hash="0" * 64,
        record_hash="0" * 64,
        evidences=(),
    )


def _subject(*, single: bool) -> dict[str, Any]:
    return {
        "finding_state_id": str(uuid.uuid4()),
        "organization_id": str(ORGANIZATION),
        "subject_kind": "finding",
        "subject_id": str(uuid.uuid4()),
        "anchor_record_id": str(uuid.uuid4()),
        "plant_id": str(PLANT),
        "zone_id": str(ZONE),
        "family": "coexistence",
        "tier": "tier_1",
        "standard_id": str(STANDARD),
        "standard_version": 1,
        "lifecycle_state": "received",
        "current_classification_outcome": None,
        "classification_count": 0,
        "due_at": None,
        "overdue_days": 0,
        "age_days": 0,
        "observability_state_at_event": "observable",
        "zone_gate_status_at_event": "approved",
        "zone_agreement_revoked": False,
        "zone_agreement_revoked_at": None,
        "single_occupancy_zone": single,
        "aggregation_window_minutes": WINDOW_MINUTES,
        "walk_test_regression_pending": False,
        "walk_test_regression_cause": None,
        "occurred_at_exact": STARTED,
        "evidence_flagged": False,
        "missing_segments_declared": False,
    }


GATE: Final = GateInterval(
    organization_id=ORGANIZATION,
    plant_id=PLANT,
    zone_id=ZONE,
    gate=GateKind.USAGE,
    status=GateStatus.APPROVED,
    effective_from=ISSUED,
    effective_until=None,
    decided_by=uuid.uuid4(),
    reason_es=None,
    record_id=None,
    ledger_record_id=uuid.uuid4(),
)


def _detail(
    versions: list[DeclaredCatalogVersion],
    *,
    single: bool = False,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """El detalle por HTTP (``detail_view``) de un registro con ``versions`` en el catálogo."""
    record = _ledger(_document() if document is None else document)
    declared = record_version(
        versions, record.document(), record_type=record.record_type, received_at=RECEIVED
    )
    inputs = DetailInputs(
        subject=_subject(single=single),
        gate=GATE,
        standard=None,
        nodes=(),
        coverage=None,
        episode=None,
        records=(record,),
        records_limited=False,
        read_urls={},
        evidence_urls_unavailable=False,
        classifications=(),
        actions=(),
        analyses=(),
        declarations={record.record_id: declared},
    )
    view: dict[str, Any] = detail_view(finding_detail(inputs)).model_dump(mode="json")
    return view


def _stamp(moment: datetime) -> str:
    return format_timestamp(moment.astimezone(UTC))


def _variables_by_code(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    (record,) = view["records"]
    return {item["code"]: item for item in record["process_context"]["variables"]}


# --- detalle con valores ------------------------------------------------------------------


def test_detail_returns_names_units_and_values_as_stored() -> None:
    view = _detail([_version(1, ISSUED)])
    (record,) = view["records"]
    block = record["process_context"]
    assert (block["from_offset_ms"], block["to_offset_ms"]) == (-30_000, 12_000)
    variables = _variables_by_code(view)
    speed = variables["VEL-MAQ"]
    assert speed == {
        "variable_id": str(SPEED),
        "kind": "analog",
        "code": "VEL-MAQ",
        "description_es": "Velocidad de la máquina",
        "unit": "m_per_min",
        "decimals": 0,
        "states": None,
        "at_event": {
            "value": 1350.25,
            "valid": True,
            "offset_ms": 0,
            "read_at": _stamp(STARTED),
        },
        "min": 1349.5,
        "max": 1401.75,
        "last": 1351.0,
        "samples": [
            {"offset_ms": -30_000, "value": 1401.75},
            {"offset_ms": -10_000, "value": None},
            {"offset_ms": 0, "value": 1350.25},
        ],
        "changes": [],
        "truncated": None,
    }
    meters = variables["MET-JUMBO"]
    assert (meters["unit"], meters["decimals"], meters["description_es"]) == (
        "m",
        1,
        "Metros del jumbo",
    )
    assert meters["at_event"] == {
        "value": None,
        "valid": False,
        "offset_ms": 1_500,
        "read_at": _stamp(STARTED + timedelta(milliseconds=1_500)),
    }
    assert (meters["min"], meters["max"], meters["last"], meters["samples"]) == (
        None,
        None,
        None,
        [],
    )
    jumbo = variables["JUMBO-POS"]
    assert (jumbo["states"], jumbo["unit"], jumbo["decimals"]) == ("in_position_out", None, None)
    assert jumbo["at_event"]["value"] == "asserted"
    assert jumbo["changes"] == [
        {"offset_ms": -30_000, "value": "deasserted"},
        {"offset_ms": -4_000, "value": "asserted"},
    ]
    assert jumbo["truncated"] is False
    crane = variables["GRUA"]
    assert (crane["states"], crane["at_event"]["value"], crane["at_event"]["valid"]) == (
        "at_rest_moving",
        "unknown",
        False,
    )
    assert crane["truncated"] is True
    # Los números salen tal como se guardaron: ni redondeo a ``decimals`` ni recálculo.
    stored = {v["variable_id"]: v for v in BLOCK["variables"]}
    assert speed["max"] == stored[str(SPEED)]["max"]
    assert [s["value"] for s in speed["samples"] if s["value"] is not None] == [1401.75, 1350.25]


def test_detail_without_block_has_no_process_context() -> None:
    view = _detail([_version(1, ISSUED)], document=_document(block=None))
    (record,) = view["records"]
    assert record["process_context"] is None
    # Las señales siguen saliendo por nombre aunque el registro no traiga valores de proceso.
    assert {s["code"] for s in record["signals"]} == {"SIG-ENE", "DI5", "DI6"}


# --- zona unipersonal ---------------------------------------------------------------------


def test_single_occupancy_aggregates_the_event_time_and_keeps_offsets() -> None:
    exact = _detail([_version(1, ISSUED)], single=False)
    single = _detail([_version(1, ISSUED)], single=True)
    assert single["time_aggregated"] is True
    meters_at = STARTED + timedelta(milliseconds=1_500)
    for code, moment in (("VEL-MAQ", STARTED), ("MET-JUMBO", meters_at)):
        aggregated = presented(moment, single_occupancy=True, window_minutes=WINDOW_MINUTES)
        assert aggregated != moment
        assert _variables_by_code(single)[code]["at_event"]["read_at"] == _stamp(aggregated)
        assert _variables_by_code(exact)[code]["at_event"]["read_at"] == _stamp(moment)
    # m_hora_exacta_en_unipersonal: ninguna hora exacta del instante del hecho en la respuesta.
    serialized = json.dumps(single)
    assert _stamp(STARTED) not in serialized and _stamp(meters_at) not in serialized
    # Los desplazamientos relativos no cambian.
    for code in ("VEL-MAQ", "MET-JUMBO", "JUMBO-POS", "GRUA"):
        a, b = _variables_by_code(single)[code], _variables_by_code(exact)[code]
        assert a["at_event"]["offset_ms"] == b["at_event"]["offset_ms"]
        assert a["samples"] == b["samples"] and a["changes"] == b["changes"]


# --- sin declaración ----------------------------------------------------------------------


def test_a_variable_no_longer_declared_comes_back_with_null_names() -> None:
    # Ninguna versión guardada declara ya la grúa: ninguna concuerda con el registro.
    versions = [_version(1, ISSUED, variables=_variables(with_crane=False))]
    view = _detail(versions)  # m_error_sin_declaracion: lanzar aquí rompe la composición
    (record,) = view["records"]
    names = [
        (item["variable_id"], item["code"], item["description_es"], item["unit"], item["states"])
        for item in record["process_context"]["variables"]
    ]
    assert names == [(item["variable_id"], None, None, None, None) for item in BLOCK["variables"]]
    # Los valores siguen: solo el nombre falta.
    assert record["process_context"]["variables"][0]["at_event"]["value"] == 1350.25
    assert {(s["code"], s["description_es"]) for s in record["signals"]} == {(None, None)}


def test_without_any_catalog_version_the_record_is_still_composed() -> None:
    view = _detail([])
    (record,) = view["records"]
    assert record["process_context"] is not None
    assert all(item["code"] is None for item in record["process_context"]["variables"])


# --- señales por nombre, de la versión del registro ---------------------------------------


def test_signals_are_named_from_the_record_version_not_the_current_one() -> None:
    later = STARTED + timedelta(days=2)
    versions = [
        _version(2, later, None, jumbo_name="Jumbo listo para cambio"),
        _version(1, ISSUED, later),
    ]
    view = _detail(versions)
    (record,) = view["records"]
    by_id = {s["signal_id"]: (s["code"], s["description_es"]) for s in record["signals"]}
    # m_catalogo_vigente: la versión 2 (vigente hoy) diría «Jumbo listo para cambio».
    assert by_id[str(JUMBO_SIGNAL)] == ("DI5", "Jumbo en posición")
    assert by_id[str(ENERGY_SIGNAL)] == ("SIG-ENE", "Energía de la máquina")


def test_record_version_is_the_newest_in_force_that_agrees() -> None:
    renamed = STARTED + timedelta(seconds=5)  # una versión nueva publicada a mitad del tramo
    versions = [
        _version(2, renamed, None, jumbo_name="Jumbo renombrado"),
        _version(1, ISSUED, renamed),
    ]
    found = record_version(
        versions, _document(), record_type="finding_received", received_at=RECEIVED
    )
    assert found is not None and found.view.version == 2  # la misma elección que el paso 7
    other = record_version(
        versions, _document(), record_type="classification", received_at=RECEIVED
    )
    assert other is None  # solo hallazgos y detecciones citan una versión


# --- ficha de vida ------------------------------------------------------------------------


def _timeline(single: bool, declared: DeclaredCatalogVersion | None) -> dict[str, Any]:
    record_id = uuid.uuid4()
    source = TimelineSource(
        record_id=record_id,
        chain_sequence=7,
        record_type="finding_received",
        received_at=RECEIVED,
        document=_document(),
    )
    timeline = finding_timeline(
        TimelineInputs(
            subject=_subject(single=single),
            records=(source,),
            truncated=False,
            configuration=None,
            timezone=None,
            declarations={record_id: declared},
        )
    )
    view: dict[str, Any] = timeline_view(timeline).model_dump(mode="json")
    return view


def test_timeline_detection_milestone_carries_the_summary_without_the_series() -> None:
    declared = _version(1, ISSUED)
    view = _timeline(single=True, declared=declared)
    (milestone,) = view["milestones"]
    summary = {item["code"]: item for item in milestone["process"]}
    assert set(summary) == {"VEL-MAQ", "MET-JUMBO", "JUMBO-POS", "GRUA"}
    speed = summary["VEL-MAQ"]
    aggregated = presented(STARTED, single_occupancy=True, window_minutes=WINDOW_MINUTES)
    assert speed == {
        "variable_id": str(SPEED),
        "kind": "analog",
        "code": "VEL-MAQ",
        "description_es": "Velocidad de la máquina",
        "unit": "m_per_min",
        "decimals": 0,
        "states": None,
        "value": 1350.25,
        "valid": True,
        "read_at": _stamp(aggregated),
    }
    assert summary["JUMBO-POS"]["value"] == "asserted"
    assert summary["JUMBO-POS"]["states"] == "in_position_out"
    for item in milestone["process"]:
        assert not {"samples", "changes", "min", "max", "last"} & set(item)
    assert _stamp(STARTED) not in json.dumps(view)


def test_timeline_without_declaration_keeps_values_with_null_names() -> None:
    view = _timeline(single=False, declared=None)
    (milestone,) = view["milestones"]
    assert {item["code"] for item in milestone["process"]} == {None}
    assert milestone["process"][0]["read_at"] == _stamp(STARTED)


# --- foto del episodio (SCR-06) -----------------------------------------------------------


def _photo(block: Any) -> EpisodePhotoSummary:
    return EpisodePhotoSummary(
        photo_id=uuid.uuid4(),
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        zone_id=ZONE,
        node_id=uuid.uuid4(),
        episode_id=uuid.uuid4(),
        segment_index=0,
        classified_as="finding",
        family="coexistence",
        standard_id=STANDARD,
        standard_version=1,
        max_confidence=0.91,
        started_at=STARTED,
        ended_at=STARTED + timedelta(seconds=12),
        received_at=RECEIVED,
        expires_at=RECEIVED + timedelta(days=30),
        camera_id=CAMERA,
        frame_at=STARTED,
        width=None,
        height=None,
        size_bytes=None,
        blur_params_version=None,
        image_absent_reason="frame_unavailable",
        process_context=block,
    )


def _photo_document(photo: EpisodePhotoSummary) -> dict[str, Any]:
    return {
        "standard": {"standard_id": str(photo.standard_id), "version": photo.standard_version},
        "camera_id": str(photo.camera_id),
        "process_context": photo.process_context,
    }


def test_photo_with_block_returns_it_named_and_without_block_null() -> None:
    later = STARTED + timedelta(days=2)
    versions = [_version(2, later, None, jumbo_name="Otro nombre"), _version(1, ISSUED, later)]
    photo = _photo(BLOCK)
    declared = photo_version(
        versions,
        _photo_document(photo),
        started_at=photo.started_at,
        ended_at=photo.ended_at,
        received_at=photo.received_at,
    )
    assert declared is not None and declared.view.version == 1
    out = photo_out(photo, declared).model_dump(mode="json")
    variables = {item["code"]: item for item in out["process_context"]["variables"]}
    assert variables["VEL-MAQ"]["description_es"] == "Velocidad de la máquina"
    assert variables["VEL-MAQ"]["samples"][0] == {"offset_ms": -30_000, "value": 1401.75}
    assert variables["VEL-MAQ"]["at_event"]["read_at"] == _stamp(STARTED)
    assert photo_out(_photo(None), None).model_dump(mode="json")["process_context"] is None


def test_photo_of_an_unsynchronized_node_falls_back_to_its_reception() -> None:
    # Solo una versión publicada después del tramo y antes de recibir la foto.
    issued = STARTED + timedelta(minutes=10)
    photo = _photo(BLOCK)
    late = EpisodePhotoSummary(**{**_fields(photo), "received_at": issued + timedelta(minutes=1)})
    declared = photo_version(
        [_version(1, issued)],
        _photo_document(late),
        started_at=late.started_at,
        ended_at=late.ended_at,
        received_at=late.received_at,
    )
    assert declared is not None and declared.view.version == 1


def _fields(photo: EpisodePhotoSummary) -> dict[str, Any]:
    return {name: getattr(photo, name) for name in EpisodePhotoSummary.__dataclass_fields__}


# --- el servicio: una consulta y degradación ----------------------------------------------


class _Catalogs:
    def __init__(self, versions: list[DeclaredCatalogVersion], *, down: bool = False) -> None:
        self.versions_list = versions
        self.down = down
        self.calls: list[Window] = []

    async def versions(
        self, context: Any, *, zone_id: uuid.UUID, window: Window
    ) -> tuple[DeclaredCatalogVersion, ...]:
        self.calls.append(window)
        assert zone_id == ZONE
        if self.down:
            raise ConnectionError("catálogo caído")
        return tuple(self.versions_list)


def _service(catalogs: _Catalogs) -> FindingDetailService:
    unused: Any = object()
    return FindingDetailService(
        database=unused,
        authorizer=unused,
        reader=unused,
        evidence=unused,
        catalog=unused,
        gates=unused,
        fleet=unused,
        coverage=unused,
        photos=unused,
        catalogs=catalogs,
    )


def _context() -> ScopeContext:
    context: ScopeContext = unit_context(ORGANIZATION, ActorUnit.U04, kind=ActorKind.SYSTEM)
    return context


def _declarations(catalogs: _Catalogs, facts: list[RecordFacts]) -> dict[uuid.UUID, Any]:
    service = _service(catalogs)
    found = asyncio.run(service.record_declarations(_context(), _subject(single=False), facts))
    return dict(found)


def _facts(count: int) -> list[RecordFacts]:
    return [
        RecordFacts(
            record_id=uuid.uuid4(),
            record_type="finding_received",
            document=_document(),
            received_at=RECEIVED,
        )
        for _ in range(count)
    ]


@pytest.mark.parametrize("count", [1, 30])
def test_record_declarations_ask_the_catalog_once_for_all_records(count: int) -> None:
    catalogs = _Catalogs([_version(1, ISSUED)])
    facts = _facts(count)
    found = _declarations(catalogs, facts)
    assert len(catalogs.calls) == 1
    assert set(found) == {f.record_id for f in facts}
    assert all(item is not None and item.view.version == 1 for item in found.values())


def test_record_declarations_degrade_to_no_names_when_the_catalog_does_not_answer() -> None:
    catalogs = _Catalogs([_version(1, ISSUED)], down=True)
    found = _declarations(catalogs, _facts(2))
    assert found == {} and len(catalogs.calls) == 1


def test_record_declarations_without_readable_time_do_not_query() -> None:
    catalogs = _Catalogs([_version(1, ISSUED)])
    facts = [RecordFacts(uuid.uuid4(), "finding_received", {"signals": []}, RECEIVED)]
    found = _declarations(catalogs, facts)
    assert found == {} and catalogs.calls == []
