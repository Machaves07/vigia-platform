"""PR-NUC-23: ``format(parse(storage_key)) = storage_key`` y la clave liga al registro (BR-NUC-65).

- Ida y vuelta sobre toda clave válida generada.
- El patrón es cerrado: anclado, UUID canónicos en minúsculas, solo ``mp4`` y ``jpg``; cada
  mutación que lo rompe se rechaza.
- Invariante: una referencia se acepta solo si su clave lleva la organización, planta, zona y
  nodo del registro, su ``clip_id`` y la extensión de su ``content_type``.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.models.clip_reference import ClipReference

from tests.properties.evidence_strategies import (
    IMAGE,
    VIDEO,
    clip_reference,
    clip_references,
    owners,
    uuid7s,
)
from vigia_platform.ledger.evidence import (
    EvidenceFailure,
    EvidenceOwner,
    StorageKey,
    StorageKeyInvalid,
    format_storage_key,
    parse_storage_key,
    storage_key_failure,
)

OWNER_FIELDS = ("organization_id", "plant_id", "zone_id", "node_id")


@st.composite
def storage_keys(draw: st.DrawFn) -> StorageKey:
    owner = draw(owners())
    return StorageKey(
        organization_id=owner.organization_id,
        plant_id=owner.plant_id,
        zone_id=owner.zone_id,
        node_id=owner.node_id,
        clip_id=draw(uuid7s()),
        ext=draw(st.sampled_from(["mp4", "jpg"])),
    )


@given(storage_keys())
def test_format_parse_round_trip(key: StorageKey) -> None:
    text = format_storage_key(key)
    assert parse_storage_key(text) == key
    assert format_storage_key(parse_storage_key(text)) == text
    assert len(text) <= 512


@given(storage_keys())
def test_key_contains_the_owner_of_the_record(key: StorageKey) -> None:
    text = format_storage_key(key)
    assert text.startswith(
        f"org/{key.organization_id}/plant/{key.plant_id}/zone/{key.zone_id}/node/{key.node_id}/"
    )
    assert parse_storage_key(text).owner == key.owner


def _mutations(text: str) -> list[str]:
    head, _, tail = text.rpartition("/")
    stem = text.rsplit(".", 1)[0]
    return [
        "",
        text.upper(),
        "/" + text,
        text + "/",
        text + " ",
        " " + text,
        text + "\n",
        text.replace("org/", "orgs/", 1),
        text.replace("/plant/", "/planta/", 1),
        text.replace("/zone/", "//zone/", 1),
        text.replace("/node/", "/node/../", 1),
        head + "/x/" + tail,
        head,
        stem,
        stem + ".MP4",
        stem + ".mov",
        stem + ".mp4.jpg",
        stem + ".%6D%70%34",
        text.replace("-", "", 1),
        text + "%00",
        "prefix/" + text,
        "documents/" + tail,
    ]


@given(storage_keys())
def test_mutated_keys_are_rejected(key: StorageKey) -> None:
    text = format_storage_key(key)
    for mutated in _mutations(text):
        assert mutated != text
        with pytest.raises(StorageKeyInvalid):
            parse_storage_key(mutated)


@given(clip_references())
def test_coherent_reference_passes(case: tuple[EvidenceOwner, ClipReference]) -> None:
    owner, reference = case
    assert storage_key_failure(owner, reference) is None


@given(clip_references(), st.sampled_from(OWNER_FIELDS), st.uuids(version=4))
def test_key_of_another_scope_is_mismatch(
    case: tuple[EvidenceOwner, ClipReference], field: str, other: uuid.UUID
) -> None:
    """La misma clave presentada por un registro de otra organización, planta, zona o nodo."""
    owner, reference = case
    values = {name: getattr(owner, name) for name in OWNER_FIELDS}
    if values[field] == other:
        return
    values[field] = other
    failure = storage_key_failure(EvidenceOwner(**values), reference)
    assert failure is EvidenceFailure.STORAGE_KEY_MISMATCH


@given(clip_references(), uuid7s())
def test_key_of_another_clip_is_mismatch(
    case: tuple[EvidenceOwner, ClipReference], other_clip: uuid.UUID
) -> None:
    owner, reference = case
    if str(other_clip) == reference.clip_id:
        return
    foreign_key = reference.storage_key.replace(reference.clip_id, str(other_clip))
    moved = reference.model_copy(update={"storage_key": foreign_key})
    assert storage_key_failure(owner, moved) is EvidenceFailure.STORAGE_KEY_MISMATCH


@given(owners(), uuid7s(), st.sampled_from([(VIDEO, "jpg"), (IMAGE, "mp4")]))
def test_extension_must_match_content_type(
    owner: EvidenceOwner, clip_id: uuid.UUID, case: tuple[str, str]
) -> None:
    content_type, wrong_ext = case
    key = format_storage_key(
        StorageKey(
            owner.organization_id, owner.plant_id, owner.zone_id, owner.node_id, clip_id, wrong_ext
        )
    )
    reference = clip_reference(
        owner,
        clip_id=clip_id,
        content_type=content_type,
        sha256="0" * 64,
        size_bytes=1,
        storage_key=key,
    )
    assert storage_key_failure(owner, reference) is EvidenceFailure.STORAGE_KEY_MISMATCH


@given(clip_references())
def test_key_outside_the_pattern_is_invalid(case: tuple[EvidenceOwner, ClipReference]) -> None:
    owner, reference = case
    for mutated in _mutations(reference.storage_key):
        # ``model_copy`` no valida: simula una referencia cuya clave el contrato admitiría.
        moved = reference.model_copy(update={"storage_key": mutated})
        assert storage_key_failure(owner, moved) is EvidenceFailure.STORAGE_KEY_INVALID


def test_format_rejects_an_unknown_extension() -> None:
    owner = EvidenceOwner(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    clip = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057")
    with pytest.raises(StorageKeyInvalid):
        format_storage_key(
            StorageKey(
                owner.organization_id, owner.plant_id, owner.zone_id, owner.node_id, clip, "png"
            )
        )
    with pytest.raises(StorageKeyInvalid):
        parse_storage_key(None)  # type: ignore[arg-type]
