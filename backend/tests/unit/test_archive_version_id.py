"""``archive_version_id_hex`` del registro ``audit_partition_archived`` (VIG-255).

El ``VersionId`` del depósito va en hexadecimal: ida y vuelta exacta para todo ASCII visible de
hasta 512 bytes, rechazo de lo que no lo es en los dos sentidos, y el contenido del registro solo
lleva la versión si el depósito la devolvió. El contenido valida contra la versión 3 del tipo y
la 2 sigue sin admitir el campo (solo se amplía).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vigia_platform.ledger.record_types.u02 import (
    AuditPartitionArchivedV2,
    AuditPartitionArchivedV3,
)
from vigia_platform.shared.archive.table_archive import (
    archived_content,
    version_id_from_hex,
    version_id_hex,
)

VISIBLE = st.text(st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=1, max_size=512)
ARCHIVED_AT = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)


@given(VISIBLE)
def test_round_trip_for_every_visible_ascii_version(version_id: str) -> None:
    encoded = version_id_hex(version_id)
    assert encoded == encoded.lower()
    assert version_id_from_hex(encoded) == version_id


def test_no_hex_means_no_version() -> None:
    assert version_id_from_hex(None) is None


@pytest.mark.parametrize(
    "version_id",
    ["", " ", "a b", "ñ", "a\n", "\x7f", "x" * 513],
)
def test_versions_that_do_not_fit_are_rejected(version_id: str) -> None:
    with pytest.raises(ValueError, match="identificador de versión no válido"):
        version_id_hex(version_id)


@pytest.mark.parametrize(
    "value",
    ["", "a", "abc", "4B69", "4b6", "zz", "20", "7f", "0a", "c3b1", "41" * 513, " 41"],
)
def test_hex_that_is_not_a_visible_version_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="archive_version_id_hex"):
        version_id_from_hex(value)


def test_the_largest_version_fits_the_record() -> None:
    encoded = version_id_hex("~" * 512)
    assert len(encoded) == 1024
    content = archived_content(
        "fleet.heartbeat_history_2026_06", "2026-06", "fleet/x.zip", "0" * 64, 1, ARCHIVED_AT, "~"
    )
    AuditPartitionArchivedV3.model_validate({**content, "archive_version_id_hex": encoded})


def test_the_content_carries_the_version_only_when_there_is_one() -> None:
    common = ("shared.audit_entry_2024_08", "2024-08", "audit/2024-08/a.zip", "a" * 64, 7)
    with_version = archived_content(*common, ARCHIVED_AT, "Ki8Yy0tVTfGwPUfjsJUSmYHONr0OcXnT")
    assert (
        with_version["archive_version_id_hex"]
        == "Ki8Yy0tVTfGwPUfjsJUSmYHONr0OcXnT".encode("ascii").hex()
    )
    assert with_version["archived_at"] == "2026-10-02T03:00:00.000Z"
    AuditPartitionArchivedV3.model_validate(with_version)
    without = archived_content(*common, ARCHIVED_AT, None)
    assert "archive_version_id_hex" not in without
    AuditPartitionArchivedV3.model_validate(without)
    # La versión 2 no conoce el campo: por eso hace falta la 3.
    with pytest.raises(ValidationError):
        AuditPartitionArchivedV2.model_validate(with_version)
    AuditPartitionArchivedV2.model_validate(without)


@pytest.mark.parametrize("value", ["4B", "", "a" * 1025, "g0"])
def test_the_record_type_closes_the_field(value: str) -> None:
    content = archived_content(
        "shared.audit_entry_2024_08", "2024-08", "audit/a.zip", "a" * 64, 1, ARCHIVED_AT, None
    )
    with pytest.raises(ValidationError):
        AuditPartitionArchivedV3.model_validate({**content, "archive_version_id_hex": value})
