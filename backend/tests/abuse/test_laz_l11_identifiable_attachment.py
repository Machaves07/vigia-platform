"""L-11: SEG-05, no se admite adjunto sin declaración; su hash debe coincidir.

Mutación: quitar el paso 7 de ``verify._steps`` (declaración firmada obligatoria).
No se afirma que una declaración sustituya la detección de rostros; es la defensa del diseño.
"""

import asyncio
import dataclasses
import uuid

import pytest

from tests.attachment_support import MemoryStore, pdf, sha256_hex, utc
from vigia_platform.loop.adapters.attachments.rules import (
    AttachmentRejected,
    attachment_storage_key,
)
from vigia_platform.loop.adapters.attachments.store import StoreCircuit
from vigia_platform.loop.adapters.attachments.verify import (
    AttachmentClaim,
    AttachmentSubject,
    ClosureAttachmentVerifier,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.shared.clock import SimulatedClock


def test_laz_l11_a_file_without_the_signed_declaration_is_not_evidence() -> None:
    subject = AttachmentSubject(*(uuid.UUID(int=n) for n in range(1, 5)))
    attachment_id = uuid.UUID(int=11)
    data = pdf()
    clock = SimulatedClock(utc())
    store = MemoryStore(clock)
    key = attachment_storage_key(
        organization_id=subject.organization_id,
        plant_id=subject.plant_id,
        zone_id=subject.zone_id,
        subject_id=subject.subject_id,
        attachment_id=attachment_id,
        content_type=AttachmentContentType.APPLICATION_PDF,
    )
    store.put(key, data, "application/pdf")
    claim = AttachmentClaim(
        attachment_id, "application/pdf", len(data), sha256_hex(data), "evidencia.pdf", False
    )
    verifier = ClosureAttachmentVerifier(store, StoreCircuit(clock))
    before = dict(store.objects)
    with pytest.raises(AttachmentRejected) as raised:
        asyncio.run(verifier.verify(subject, [claim]))
    assert raised.value.detail is LoopDetailCode.ATTACHMENT_DECLARATION_MISSING
    assert store.objects == before
    with pytest.raises(AttachmentRejected) as raised:
        asyncio.run(
            verifier.verify(
                subject,
                [dataclasses.replace(claim, no_identifiable_person_declared=True, sha256="0" * 64)],
            )
        )
    assert raised.value.detail is LoopDetailCode.ATTACHMENT_HASH_MISMATCH
    assert (
        len(
            asyncio.run(
                verifier.verify(
                    subject, [dataclasses.replace(claim, no_identifiable_person_declared=True)]
                )
            )
        )
        == 1
    )
