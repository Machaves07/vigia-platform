"""La ficha de vida con los adjuntos del cierre (TASK-609; O-22; BR-APP-40 y 58; BR-LAZ-09).

Composición pura y esquema de la respuesta, sin base de datos (el recorrido por las rutas reales,
con la auditoría contada, está en ``tests/integration/test_loop_timeline_routes.py``):

- **Referencias**: cada ``action_evidence_added`` lleva ``attachment_id``, tipo y tamaño de sus
  adjuntos, en el orden del registro; el ``closure``, los que cierra (sus ``attachment_digests``
  resueltas contra las evidencias del sujeto), en el orden de las sumas y sin repetir ninguno. Una
  suma que la ficha no trae (truncada) no se inventa.
- **Minimización** (metapropiedad sobre el esquema de la respuesta, como PR-LAZ-01): ningún campo
  de ``FindingTimelineOut``, recursivamente, es una URL, una clave de almacén, una suma o un
  nombre de archivo; una sonda con ``filename`` cae. Y por valor: ni las sumas ni los nombres de
  archivo de los registros aparecen en la respuesta serializada.
- **50 adjuntos**: diez evidencias con cinco adjuntos cada una dan 50 referencias con exactamente
  los tres campos, en **una** lectura del expediente.

Solo datos generados; los bytes de los adjuntos son sintéticos y solo sirven para sus sumas.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, Final

from pydantic import BaseModel, create_model

from tests.properties.laz.test_pr_laz_01_no_person_parameters import _schema_names, _words
from tests.unit.test_loop_timeline import (
    ACTION,
    SUBJECT,
    T0,
    _run_service,
    _view,
    compose,
    cycle,
)
from vigia_platform.ledger.application.reader import SubjectHistory
from vigia_platform.loop.adapters.http.timeline_routes import (
    FindingTimelineOut,
    TimelineAttachmentOut,
    TimelineMilestoneOut,
    timeline_view,
)
from vigia_platform.loop.application.timeline import (
    MilestoneAttachment,
    MilestoneKind,
    TimelineSource,
)

FORBIDDEN_WORDS: Final = frozenset(
    {
        # dónde está el objeto
        "url",
        "uri",
        "href",
        "link",
        "storage",
        "bucket",
        "object",
        # la suma
        "sha",
        "sha256",
        "digest",
        "digests",
        "checksum",
        # el nombre del archivo (un campo de persona del lazo, record_types.py)
        "filename",
        "file",
    }
)
"""Palabras de un campo que diría dónde está un adjunto, su suma o su nombre de archivo."""

REFERENCE_FIELDS: Final = frozenset({"attachment_id", "content_type", "size_bytes"})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attachment(n: int, content_type: str, filename: str) -> dict[str, Any]:
    data = f"adjunto sintético {n} de {content_type}".encode()
    return {
        "attachment_id": str(uuid.UUID(f"0192d3e4-5f60-7a1b-8c2d-{n:012d}")),
        "sha256": _sha(data),
        "content_type": content_type,
        "size_bytes": 1000 + n,
        "filename": filename,
        "no_identifiable_person_declared": True,
    }


JPG: Final = _attachment(1, "image/jpeg", "resguardo-instalado.jpg")
PDF: Final = _attachment(2, "application/pdf", "acta-de-verificacion.pdf")
PNG: Final = _attachment(3, "image/png", "senalizacion-repintada.png")


def _reference(document: dict[str, Any]) -> MilestoneAttachment:
    return MilestoneAttachment(
        attachment_id=uuid.UUID(document["attachment_id"]),
        content_type=document["content_type"],
        size_bytes=document["size_bytes"],
    )


def with_attachments(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    digests: list[str],
) -> list[TimelineSource]:
    """El ciclo con los adjuntos de sus dos evidencias y las sumas que cita el cierre."""
    evidences = iter((first, second))
    records: list[TimelineSource] = []
    for record in cycle():
        if record.record_type == "action_evidence_added":
            record = replace(record, document={**record.document, "attachments": next(evidences)})
        elif record.record_type == "closure":
            record = replace(record, document={**record.document, "attachment_digests": digests})
        records.append(record)
    return records


def _by_kind(
    records: list[TimelineSource],
) -> dict[MilestoneKind, list[tuple[MilestoneAttachment, ...]]]:
    found: dict[MilestoneKind, list[tuple[MilestoneAttachment, ...]]] = {}
    for milestone in compose(records).milestones:
        found.setdefault(milestone.kind, []).append(milestone.attachments)
    return found


# --- referencias -------------------------------------------------------------------------------


def test_evidences_carry_their_attachments_in_order_and_the_closure_those_it_closes() -> None:
    records = with_attachments([JPG, PDF], [PNG], [PDF["sha256"], JPG["sha256"]])
    found = _by_kind(records)
    assert found[MilestoneKind.ACTION_EVIDENCE_ADDED] == [
        (_reference(JPG), _reference(PDF)),
        (_reference(PNG),),
    ]
    # m_sin_adjuntos_del_cierre: el cierre trae los que cierra, en el orden de sus sumas.
    assert found[MilestoneKind.CLOSURE] == [(_reference(PDF), _reference(JPG))]
    # Ningún otro hito lleva adjuntos.
    others = [
        attachments
        for kind, lists in found.items()
        if kind not in (MilestoneKind.ACTION_EVIDENCE_ADDED, MilestoneKind.CLOSURE)
        for attachments in lists
    ]
    assert others and all(attachments == () for attachments in others)


def test_the_closure_never_repeats_an_attachment() -> None:
    # La misma suma citada dos veces y el mismo archivo subido en las dos evidencias: cada
    # adjunto, una vez.
    again = {**JPG, "attachment_id": str(uuid.UUID("0192d3e4-5f60-7a1b-8c2d-000000000009"))}
    records = with_attachments([JPG, PDF], [again], [JPG["sha256"], PDF["sha256"], JPG["sha256"]])
    (closed,) = _by_kind(records)[MilestoneKind.CLOSURE]
    ids = [attachment.attachment_id for attachment in closed]
    assert ids == [
        uuid.UUID(JPG["attachment_id"]),
        uuid.UUID(again["attachment_id"]),
        uuid.UUID(PDF["attachment_id"]),
    ]
    assert len(set(ids)) == len(ids)


def test_a_digest_the_timeline_does_not_carry_is_not_invented() -> None:
    records = with_attachments([JPG], [], [PDF["sha256"], JPG["sha256"]])
    assert _by_kind(records)[MilestoneKind.CLOSURE] == [(_reference(JPG),)]
    # Un cierre sin adjuntos ni sumas (el de U-04 sin evidencia gráfica): ninguno.
    plain = [replace(r, document={**r.document, "attachment_digests": []}) for r in records]
    assert _by_kind(plain)[MilestoneKind.CLOSURE] == [()]


# --- minimización ------------------------------------------------------------------------------


def _forbidden(model: type[BaseModel]) -> list[str]:
    return sorted(
        name
        for name in set(_schema_names(model.model_json_schema()))
        if _words(name) & FORBIDDEN_WORDS
    )


def test_no_field_of_the_response_is_a_url_a_storage_key_a_digest_or_a_filename() -> None:
    assert _forbidden(FindingTimelineOut) == []
    # La referencia, exactamente sus tres campos.
    assert set(TimelineAttachmentOut.model_fields) == REFERENCE_FIELDS


def test_the_metaproperty_catches_a_probe_with_a_filename_or_a_url() -> None:
    """m_con_nombre_de_archivo y m_url_en_la_ficha: un campo así en la referencia cae."""
    for name in ("filename", "url", "storage_key", "sha256"):
        probe = create_model(  # type: ignore[call-overload]
            "ProbeAttachmentOut", __base__=TimelineAttachmentOut, **{name: (str, ...)}
        )
        milestone = create_model(
            "ProbeMilestoneOut",
            __base__=TimelineMilestoneOut,
            attachments=(tuple[probe, ...], ...),
        )
        assert _forbidden(milestone) == [name], name


def _values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _values(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _values(item)
    else:
        yield value


def test_neither_the_digests_nor_the_filenames_of_the_records_reach_the_response() -> None:
    records = with_attachments([JPG, PDF], [PNG], [JPG["sha256"], PDF["sha256"]])
    body = timeline_view(compose(records)).model_dump(mode="json")
    values = set(_values(body))
    for attachment in (JPG, PDF, PNG):
        assert attachment["sha256"] not in values
        assert attachment["filename"] not in values
    references = [
        reference for milestone in body["milestones"] for reference in milestone["attachments"]
    ]
    assert [r["attachment_id"] for r in references] == [
        JPG["attachment_id"],
        PDF["attachment_id"],
        PNG["attachment_id"],
        JPG["attachment_id"],
        PDF["attachment_id"],
    ]
    assert all(set(r) == REFERENCE_FIELDS for r in references)


# --- 50 adjuntos, una lectura ------------------------------------------------------------------


def _evidence(index: int) -> TimelineSource:
    attachments = [
        _attachment(index * 10 + n, ("image/jpeg", "application/pdf")[n % 2], f"adjunto-{n}.jpg")
        for n in range(5)
    ]
    return TimelineSource(
        record_id=uuid.UUID(int=5000 + index),
        chain_sequence=200 + index,
        record_type="action_evidence_added",
        received_at=T0 + timedelta(hours=index + 1),
        document={
            "evidence_entry_id": str(uuid.UUID(int=6000 + index)),
            "action_id": str(ACTION),
            "subject_id": str(SUBJECT),
            "description": "Evidencia sintética con cinco adjuntos",
            "performed_at": (T0 + timedelta(hours=index)).isoformat(),
            "performed_by_role": "Técnico de mantenimiento",
            "attachments": attachments,
            "added_by": {
                "user_id": str(uuid.UUID(int=900)),
                "display_name": "Coordinación SST sintética",
                "role_in_use": "coordinator_sst",
            },
            "added_at": (T0 + timedelta(hours=index + 1)).isoformat(),
        },
    )


def test_fifty_attachments_are_fifty_references_in_one_ledger_read() -> None:
    received = cycle()[0]
    records = [received, *(_evidence(index) for index in range(10))]
    history = SubjectHistory(
        records=tuple(_view(r) for r in records),
        truncated=False,
        preceding=(),
        preceding_complete=True,
    )
    timeline, reader = _run_service(history)
    assert len(reader.calls) == 1
    body = timeline_view(timeline).model_dump(mode="json")
    references = [r for m in body["milestones"] for r in m["attachments"]]
    assert len(references) == 50
    assert len({r["attachment_id"] for r in references}) == 50
    assert all(set(r) == REFERENCE_FIELDS for r in references)
