"""PR-GOB-02 de la ingesta: el código es el del paso de **menor índice** (BR-GOB-84; BR-CTR-32).

Completa la parte común de ``test_pr_gob_02_prechecks_order.py`` (TASK-206, pasos 1 a 4 con una ruta
de prueba) con los nueve pasos sobre las tres rutas **reales** de la ingesta (TASK-221). Para toda
presentación válida del kit de U-01 y todo conjunto de violaciones (``violation_combinations``,
porque el kit fijado no trae ``mutate_submission`` ni ``violation_combinations``):

1. versión: sin cabecera o una menor más nueva (``rejected_newer`` como ``compatibility_result``);
2. certificado y alcance: nodo revocado, **o** la parte que depende del cuerpo: zona de otra
   organización, zona de la organización nunca asignada al nodo, zona asignada **ahora** pero no en
   ``node_time.started_at``, u organización del cuerpo distinta del certificado;
3. tamaño: cuerpo por encima del límite de la ruta;
4. esquema: un campo no declarado;
5. idempotencia: un registro aceptado con la misma clave y otro contenido;
6. clips: un clip citado sin objeto (solo si el registro cita alguno);
7. catálogo: un estándar que ningún catálogo vigente cita, o un registro más antiguo que la
   retención (``timestamp_out_of_window``; el único del paso 7 en los eventos);
8. compuerta: el uso nunca aprobado (solo hallazgos y detecciones).

**Oráculo**: el de menor índice, con una sola salvedad declarada en TASK-221: un cuerpo por encima
del límite no se lee, así que la parte del paso 2 que depende del cuerpo no se evalúa y decide el
tamaño (``si ellos mismos no se pueden leer, decide el paso siguiente``); el certificado revocado sí
gana al tamaño. Además: nada se escribe, ``ingest_rejected`` solo con ``node_zone_mismatch`` y
``zone_gate_not_approved``, y todo rechazo con la identidad resuelta deja su auditoría.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    observability_event,
    zone_catalog,
)
from vigia_contracts.models.api import parse_rejection_response

from tests.ingest_support import IngestWorld, ingest_world, place
from tests.node_api_support import VERSION
from vigia_platform.fleet.adapters.postgres.ingest_queries import AcceptedRecord
from vigia_platform.fleet.domain.ingest_order import (
    STEP_CODES,
    AssignmentSpan,
    IngestKind,
    IngestStep,
    cited_clip_ids,
)
from vigia_platform.ledger.application.writer import LedgerRejectionCode

HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)
YEAR = dt.timedelta(days=365)
_MAJOR, _MINOR = (int(part) for part in VERSION.split("-")[0].split("+")[0].split(".")[:2])

CERTIFICATE_FLAVOURS = ("revoked", "foreign_zone", "other_zone", "not_at_instant", "organization")
BODY_FLAVOURS = frozenset(CERTIFICATE_FLAVOURS) - {"revoked"}


def _strategy(kind: IngestKind, catalog: dict[str, Any], node_id: str) -> st.SearchStrategy[Any]:
    if kind is IngestKind.FINDING:
        return finding(catalog, node_id=node_id)
    if kind is IngestKind.DETECTION_FOR_REVIEW:
        return detection_for_review(catalog, node_id=node_id)
    return observability_event(catalog, node_id=node_id)


def violation_combinations(
    kind: IngestKind, has_clips: bool
) -> st.SearchStrategy[frozenset[IngestStep]]:
    """Subconjuntos (también vacío) de los pasos que se pueden romper para ``kind``."""
    steps = [step for step in IngestStep if step is not IngestStep.WRITE]
    if not kind.gated:
        steps.remove(IngestStep.GATE)
    if not has_clips:
        steps.remove(IngestStep.CLIPS)
    return st.sets(st.sampled_from(steps)).map(frozenset)


@st.composite
def scenarios(draw: st.DrawFn) -> dict[str, Any]:
    kind = draw(st.sampled_from(tuple(IngestKind)))
    return {
        "kind": kind,
        "catalog": draw(zone_catalog()),
        "version": draw(st.sampled_from([None, f"{_MAJOR}.{_MINOR + 1}.0"])),
        "certificate": draw(st.sampled_from(CERTIFICATE_FLAVOURS)),
        "catalog_flavour": draw(
            st.sampled_from(["standard", "too_old"] if kind.gated else ["too_old"])
        ),
        "padding": draw(st.integers(1, 2_048)),
    }


def _prepare(
    data: st.DataObject, scenario: dict[str, Any]
) -> tuple[IngestWorld, dict[str, Any], frozenset[IngestStep]]:
    kind: IngestKind = scenario["kind"]
    world = ingest_world()
    # Asignada desde hace dos años: un registro de hace 100 días (paso 7) sigue en su zona.
    world.store.assignment_rows = [
        (world.a.node_id, AssignmentSpan(world.zone, world.now - 2 * YEAR, None))
    ]
    catalog = world.scoped(scenario["catalog"])
    world.publish_catalog(catalog, world.now - YEAR)
    document = data.draw(_strategy(kind, catalog, str(world.a.node_id)), label="document")
    document = place(document, world.now - HOUR)
    violated = data.draw(
        violation_combinations(kind, bool(cited_clip_ids(document))), label="violated"
    )
    if IngestStep.GATE not in violated:
        world.set_usage(True, world.now - YEAR)
    return world, document, violated


def _apply(
    world: IngestWorld,
    kind: IngestKind,
    document: dict[str, Any],
    violated: frozenset[IngestStep],
    scenario: dict[str, Any],
) -> tuple[dict[str, str], bytes]:
    flavour = scenario["certificate"]
    if IngestStep.CATALOG in violated:
        if scenario["catalog_flavour"] == "standard":
            standard = dict(document["standard"])
            standard["version"] = standard["version"] + 1000
            document["standard"] = standard
        else:
            started = world.now - 100 * DAY
            document.update(place(document, started))
    if IngestStep.IDEMPOTENCY in violated:
        stored = copy.deepcopy(document)
        stored["software_version"] = "9.8.7" if document["software_version"] != "9.8.7" else "9.8.6"
        stored["receipt"] = {
            "platform_record_id": str(uuid.uuid4()),
            "received_at": "2026-01-01T00:00:00.000Z",
            "status": "accepted",
        }
        key = (world.a.organization_id, kind.record_type, document[kind.id_field])
        world.store.records[key] = AcceptedRecord(uuid.uuid4(), world.now, stored)
    if IngestStep.CLIPS in violated:
        world.evidence.outcomes[cited_clip_ids(document)[0]] = LedgerRejectionCode.EVIDENCE_MISSING
    if IngestStep.CERTIFICATE in violated:
        if flavour == "revoked":
            world.a.node_status, world.a.revoked_at = "revoked", world.now
        elif flavour == "foreign_zone":
            document["zone_id"] = str(world.foreign_zone)
        elif flavour == "other_zone":
            document["zone_id"] = str(world.other_zone)
        elif flavour == "not_at_instant":
            world.store.assignment_rows = [
                (
                    world.a.node_id,
                    AssignmentSpan(world.zone, world.now - dt.timedelta(minutes=10), None),
                )
            ]
        else:
            document["organization_id"] = str(uuid.uuid4())
    if IngestStep.SCHEMA in violated:
        document["campo_no_declarado"] = 1
    version = scenario["version"] if IngestStep.VERSION in violated else VERSION
    headers = world.headers(document, kind, version=version)
    body = json.dumps(document, separators=(",", ":")).encode()
    if IngestStep.SIZE in violated:
        limit = {True: 262_144, False: 65_536}[kind.gated]
        body = body + b" " * (limit - len(body) + scenario["padding"])
    return headers, body


def expected_step(violated: frozenset[IngestStep], flavour: str) -> IngestStep | None:
    """El oráculo: el menor índice, sin la parte del paso 2 que depende de un cuerpo ilegible."""
    effective = set(violated)
    if IngestStep.SIZE in violated and flavour in BODY_FLAVOURS:
        effective.discard(IngestStep.CERTIFICATE)
    return min(effective) if effective else None


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(data=st.data(), scenario=scenarios())
def test_pr_gob_02_the_code_is_the_one_of_the_lowest_violated_step(
    data: st.DataObject, scenario: dict[str, Any]
) -> None:
    kind: IngestKind = scenario["kind"]
    world, document, violated = _prepare(data, scenario)
    headers, body = _apply(world, kind, document, violated, scenario)
    before = dict(world.store.records)
    response = world.post(kind, document, headers=headers, body=body)
    first = expected_step(violated, scenario["certificate"])
    if first is None:
        assert response.status_code == 200, response.text
        assert len(world.accepted(kind)) == 1
        assert world.audit.entries == [] and world.writer.rejections == []
        return
    assert response.status_code != 200, (sorted(violated), response.text)
    rejection = parse_rejection_response(response.content)
    code = rejection.code
    assert code in STEP_CODES[first], (sorted(violated), scenario["certificate"], response.text)
    if first is IngestStep.CATALOG:
        expected = (
            "schema_invalid"
            if scenario["catalog_flavour"] == "standard"
            else ("timestamp_out_of_window")
        )
        assert code.value == expected
        if expected == "schema_invalid":
            assert rejection.field == "standard"
    if first is IngestStep.VERSION:
        assert code.value != "rejected_newer"
    if first is IngestStep.SCHEMA:
        assert code.value == "schema_invalid"
    # Nada se acepta a medias.
    assert world.store.records == before
    assert world.writer.events == []
    # ``ingest_rejected`` solo con los dos códigos, sin el contenido (BR-GOB-96).
    if code.value in {"node_zone_mismatch", "zone_gate_not_approved"}:
        (written,) = world.writer.rejections
        assert set(written) <= {
            "node_id",
            "zone_id",
            "record_kind",
            "code",
            "correlation_id",
            "received_at",
        }
        assert written["code"] == code.value
        assert written["node_id"] == str(world.a.node_id)
        if "zone_id" in written:
            assert written["zone_id"] in {str(world.zone), str(world.other_zone)}
    else:
        assert world.writer.rejections == []
    # Todo rechazo con la identidad del nodo resuelta deja su auditoría (no el del nodo revocado).
    revoked = IngestStep.CERTIFICATE in violated and scenario["certificate"] == "revoked"
    if revoked:
        assert world.audit.entries == []
    else:
        (entry,) = world.audit.entries
        assert entry["filters"] == {"record_kind": kind.record_kind.value, "code": code.value}
        assert entry["plant_id"] == world.a.plant_id
        assert entry["zone_id"] != world.foreign_zone
