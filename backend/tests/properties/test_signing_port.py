"""PR-NUC-34: ``verify(sign(p, x))`` es verdadero, y falso con la clave de otro propósito o con
cualquier bit alterado (TASK-115; BR-NUC-84 y 87; LC-NUC-25).

Para ``catalog``, ``gate`` y ``key_set`` la verificación es la de U-01 (``vigia_contracts.signing
.verify`` con un ``KeySet`` fijado con las claves publicadas): la misma que hace el nodo;
``key_set`` no se firma por ``sign`` sino en la rotación, y se prueba sobre la publicación. Para
``checkpoint`` es ``verify_platform_envelope`` (la del verificador de paquetes) y para
``live_view_token`` la firma de la entrada JWS con ``verify_detached``.

Además: el material privado nunca aparece en un ``repr``, un sobre, una clave publicada, un
registro del expediente ni el registro de la aplicación; y la verificación falla cerrada ante
entradas hostiles (sin excepciones). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.canonical import canonicalize
from vigia_contracts.conformance.generators import zone_catalog
from vigia_contracts.conformance.stub_platform.heartbeat import resulting_mode
from vigia_contracts.models.enumerations import GateStatus
from vigia_contracts.signing import KeySet, SignatureInvalidError, verify

from tests.signing_support import SigningWorld, bootstrapped_world, provider_context
from vigia_platform.shared.signing import (
    NODE_PURPOSES,
    PlatformSignedEnvelope,
    SigningKeyRecord,
    SigningKeyUnavailable,
    SigningNotReady,
    SigningPurpose,
    SigningService,
    verify_detached,
    verify_platform_envelope,
)
from vigia_platform.shared.signing.keys import active_key, format_timestamp

CONTRACT_PURPOSES = (SigningPurpose.CATALOG, SigningPurpose.GATE)
"""Propósitos que se firman por ``sign``; ``key_set`` solo lo firma la rotación."""
SAFE_INT = 2**53 - 1


@pytest.fixture(scope="module")
def world() -> Iterator[SigningWorld]:
    yield asyncio.run(bootstrapped_world())


def _active(world: SigningWorld, purpose: SigningPurpose) -> SigningKeyRecord:
    key = active_key(world.service.all_keys(), purpose)
    assert key is not None
    return key


def _node_keyset(world: SigningWorld) -> KeySet:
    keyset = KeySet(world.clock)
    keyset.pin_initial(
        [k.to_contract() for p in NODE_PURPOSES for k in world.service.public_keys(p)]
    )
    return keyset


def _flip_bit(data: bytes, bit: int) -> bytes:
    index = bit % (len(data) * 8)
    flipped = bytearray(data)
    flipped[index // 8] ^= 1 << (index % 8)
    return bytes(flipped)


def _flip_signature(signature: str, bit: int) -> str:
    return base64.b64encode(_flip_bit(base64.b64decode(signature), bit)).decode("ascii")


# --- Generadores de cargas ------------------------------------------------------------------


json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-SAFE_INT, max_value=SAFE_INT),
    st.text(max_size=40),
)
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=12), children, max_size=4),
    ),
    max_leaves=20,
)
json_objects = st.dictionaries(st.text(max_size=12), json_values, min_size=1, max_size=6)

timestamps = st.datetimes(
    min_value=datetime(2026, 1, 1),  # noqa: DTZ001 - Hypothesis exige límites sin zona
    max_value=datetime(2030, 12, 31),  # noqa: DTZ001 - la zona la pone timezones
    timezones=st.just(UTC),
)


@st.composite
def gate_states(draw: st.DrawFn) -> dict[str, Any]:
    issued = draw(timestamps)
    mounting = draw(st.sampled_from(GateStatus))
    usage = draw(st.sampled_from(GateStatus))
    return {
        "zone_id": str(draw(st.uuids(version=4))),
        "organization_id": str(draw(st.uuids(version=4))),
        "plant_id": str(draw(st.uuids(version=4))),
        "mounting_gate": {"status": mounting.value},
        "usage_gate": {"status": usage.value},
        "resulting_mode": resulting_mode(mounting, usage).value,
        "issued_at": format_timestamp(issued),
        "valid_until": format_timestamp(issued + timedelta(days=7)),
    }


def _payloads(purpose: SigningPurpose) -> st.SearchStrategy[dict[str, Any]]:
    if purpose is SigningPurpose.CATALOG:
        return zone_catalog()
    return gate_states()


# --- PR-NUC-34 --------------------------------------------------------------------------------


@given(data=st.data())
def test_contract_envelope_verifies_only_with_its_purpose_and_intact_bits(
    world: SigningWorld, data: st.DataObject
) -> None:
    purpose = data.draw(st.sampled_from(CONTRACT_PURPOSES), label="purpose")
    payload = data.draw(_payloads(purpose), label="payload")
    envelope = world.service.sign(purpose, payload)
    document = envelope.model_dump(mode="json", exclude_none=True)
    keyset = _node_keyset(world)

    # Ida y vuelta: el verificador de U-01 (el del nodo) devuelve la carga.
    assert verify(document, keyset, purpose, world.clock) == document["payload"]
    assert document["key_id"] == _active(world, purpose).key_id

    message = canonicalize(document["payload"])
    for other in SigningPurpose:
        if other is purpose:
            continue
        other_key = _active(world, other)
        # La clave de otro propósito no verifica la firma…
        assert not verify_detached(other_key.public_key, message, document["signature"])
        # …ni un sobre que la cite en lugar de la del propósito.
        if other in NODE_PURPOSES:
            with pytest.raises(SignatureInvalidError):
                verify({**document, "key_id": other_key.key_id}, keyset, purpose, world.clock)

    bit = data.draw(st.integers(min_value=0, max_value=511), label="bit de la firma")
    tampered = {**document, "signature": _flip_signature(document["signature"], bit)}
    with pytest.raises(SignatureInvalidError):
        verify(tampered, keyset, purpose, world.clock)
    message_bit = data.draw(st.integers(min_value=0), label="bit de la carga")
    assert not verify_detached(
        _active(world, purpose).public_key, _flip_bit(message, message_bit), document["signature"]
    )


@given(payload=json_objects, data=st.data())
def test_checkpoint_envelope_verifies_only_with_checkpoint_keys_and_intact_bits(
    world: SigningWorld, payload: dict[str, Any], data: st.DataObject
) -> None:
    envelope = world.service.sign(SigningPurpose.CHECKPOINT, payload)
    assert isinstance(envelope, PlatformSignedEnvelope)
    keys = world.service.all_keys()
    published = world.service.public_keys(SigningPurpose.CHECKPOINT)
    assert verify_platform_envelope(envelope, published, SigningPurpose.CHECKPOINT)
    assert verify_platform_envelope(envelope.to_json(), keys, SigningPurpose.CHECKPOINT)

    document = envelope.to_json()
    for other in SigningPurpose:
        if other is SigningPurpose.CHECKPOINT:
            continue
        other_key = _active(world, other)
        assert not verify_platform_envelope(document, keys, other)
        swapped = {**document, "key_id": other_key.key_id}
        assert not verify_platform_envelope(swapped, keys, SigningPurpose.CHECKPOINT)
        assert not verify_detached(
            other_key.public_key, canonicalize(payload), document["signature"]
        )

    bit = data.draw(st.integers(min_value=0, max_value=511), label="bit de la firma")
    tampered = {**document, "signature": _flip_signature(document["signature"], bit)}
    assert not verify_platform_envelope(tampered, keys, SigningPurpose.CHECKPOINT)
    digest_bit = data.draw(st.integers(min_value=0, max_value=255), label="bit del resumen")
    digest = bytes.fromhex(document["payload_canonical_sha256"])
    altered_digest = {**document, "payload_canonical_sha256": _flip_bit(digest, digest_bit).hex()}
    assert not verify_platform_envelope(altered_digest, keys, SigningPurpose.CHECKPOINT)
    extra = data.draw(st.text(min_size=1, max_size=8).filter(lambda k: k not in payload))
    altered_payload = {**document, "payload": {**payload, extra: 0}}
    assert not verify_platform_envelope(altered_payload, keys, SigningPurpose.CHECKPOINT)


@given(
    rotations=st.lists(st.sampled_from(sorted(NODE_PURPOSES)), min_size=1, max_size=3),
    data=st.data(),
)
def test_key_set_publication_verifies_only_with_its_key_and_intact_bits(
    rotations: list[SigningPurpose], data: st.DataObject
) -> None:
    """``key_set`` se firma solo por la rotación: su sobre (``current_key_set_envelope``) cumple
    PR-NUC-34 con el verificador de U-01, igual que los que salen de ``sign``."""

    async def scenario() -> SigningWorld:
        world = await bootstrapped_world()
        for purpose in rotations:
            world.clock.advance(1)
            await world.service.rotate(purpose, context=provider_context())
        return world

    world = asyncio.run(scenario())
    envelope = world.service.current_key_set_envelope()
    assert envelope is not None
    document = envelope.model_dump(mode="json", exclude_none=True)
    keyset = _node_keyset(world)
    signer = next(k for k in world.service.all_keys() if k.key_id == document["key_id"])
    assert signer.purpose is SigningPurpose.KEY_SET
    assert verify(document, keyset, SigningPurpose.KEY_SET, world.clock) == document["payload"]

    message = canonicalize(document["payload"])
    for other in SigningPurpose:
        if other is SigningPurpose.KEY_SET:
            continue
        other_key = _active(world, other)
        assert not verify_detached(other_key.public_key, message, document["signature"])
        if other in NODE_PURPOSES:
            with pytest.raises(SignatureInvalidError):
                verify(
                    {**document, "key_id": other_key.key_id},
                    keyset,
                    SigningPurpose.KEY_SET,
                    world.clock,
                )
    bit = data.draw(st.integers(min_value=0, max_value=511), label="bit de la firma")
    tampered = {**document, "signature": _flip_signature(document["signature"], bit)}
    with pytest.raises(SignatureInvalidError):
        verify(tampered, keyset, SigningPurpose.KEY_SET, world.clock)
    message_bit = data.draw(st.integers(min_value=0), label="bit de la carga")
    assert not verify_detached(
        signer.public_key, _flip_bit(message, message_bit), document["signature"]
    )


@given(message=st.binary(min_size=1, max_size=512), data=st.data())
def test_live_view_token_signature_verifies_only_with_its_key_and_intact_bits(
    world: SigningWorld, message: bytes, data: st.DataObject
) -> None:
    detached = world.service.sign_detached(SigningPurpose.LIVE_VIEW_TOKEN, message)
    signature = base64.b64encode(detached.signature).decode("ascii")
    own = _active(world, SigningPurpose.LIVE_VIEW_TOKEN)
    assert detached.key_id == own.key_id
    assert verify_detached(own.public_key, message, signature)
    for other in SigningPurpose:
        if other is not SigningPurpose.LIVE_VIEW_TOKEN:
            assert not verify_detached(_active(world, other).public_key, message, signature)
    bit = data.draw(st.integers(min_value=0, max_value=511), label="bit de la firma")
    assert not verify_detached(own.public_key, message, _flip_signature(signature, bit))
    message_bit = data.draw(st.integers(min_value=0), label="bit del mensaje")
    assert not verify_detached(own.public_key, _flip_bit(message, message_bit), signature)


# --- Fallo cerrado ante entradas hostiles -----------------------------------------------------

hostile_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=80),
        st.binary(max_size=40),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=15,
)


@given(
    envelope=st.one_of(
        hostile_values,
        st.fixed_dictionaries(
            {
                "payload": hostile_values,
                "payload_canonical_sha256": st.one_of(st.text(max_size=70), hostile_values),
                "signature": st.one_of(st.text(max_size=100), hostile_values),
                "key_id": st.one_of(st.text(max_size=70), hostile_values),
                "signed_at": hostile_values,
            }
        ),
    ),
    public_key=st.one_of(st.text(max_size=60), st.just("A" * 43 + "=")),
    signature=st.one_of(st.text(max_size=100), st.just("A" * 86 + "==")),
)
def test_verification_fails_closed_on_hostile_input(
    world: SigningWorld, envelope: Any, public_key: str, signature: str
) -> None:
    keys = world.service.all_keys()
    for purpose in SigningPurpose:
        assert verify_platform_envelope(envelope, keys, purpose) is False
    assert verify_detached(public_key, b"mensaje", signature) is False


FIELD_PRIME = 2**255 - 19
ORDER_8_Y = int.from_bytes(
    bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"), "little"
)


@pytest.mark.parametrize(
    "y", [0, 1, FIELD_PRIME - 1, ORDER_8_Y, FIELD_PRIME - ORDER_8_Y, FIELD_PRIME, FIELD_PRIME + 1]
)
@pytest.mark.parametrize("sign_bit", [0, 1])
def test_small_order_and_non_canonical_public_keys_never_verify(y: int, sign_bit: int) -> None:
    """Regresión permanente (contraejemplo reducido de la propiedad hostil): con la clave toda a
    ceros y la firma toda a ceros, ``cryptography`` verificaba cualquier mensaje."""
    raw = (y | sign_bit << 255).to_bytes(32, "little")
    public_key = base64.b64encode(raw).decode("ascii")
    zero_signature = base64.b64encode(bytes(64)).decode("ascii")
    identity_signature = base64.b64encode((1).to_bytes(32, "little") + bytes(32)).decode("ascii")
    for signature in (zero_signature, identity_signature):
        for message in (b"mensaje", b"", b"\x00" * 64):
            assert verify_detached(public_key, message, signature) is False


def test_verification_fails_closed_on_deep_nesting_huge_numbers_and_nan(
    world: SigningWorld,
) -> None:
    key = _active(world, SigningPurpose.CHECKPOINT)
    deep: Any = 0
    for _ in range(50_000):
        deep = [deep]
    for payload in (deep, 10**400, math.nan, math.inf, {"x": -math.inf}):
        envelope = {
            "payload": payload,
            "payload_canonical_sha256": "0" * 64,
            "signature": "A" * 86 + "==",
            "key_id": key.key_id,
            "signed_at": "2026-09-30T12:00:00.000Z",
        }
        assert verify_platform_envelope(envelope, [key], SigningPurpose.CHECKPOINT) is False


# --- Uso del puerto ---------------------------------------------------------------------------


def test_each_purpose_is_signed_only_through_its_own_path(world: SigningWorld) -> None:
    with pytest.raises(ValueError, match="sign_detached"):
        world.service.sign(SigningPurpose.LIVE_VIEW_TOKEN, {"sub": "x"})
    for purpose in SigningPurpose:
        if purpose is not SigningPurpose.LIVE_VIEW_TOKEN:
            with pytest.raises(ValueError, match="live_view_token"):
                world.service.sign_detached(purpose, b"entrada")
    with pytest.raises(ValueError, match="bytes no vac"):
        world.service.sign_detached(SigningPurpose.LIVE_VIEW_TOKEN, b"")
    # Una carga que no es del tipo del propósito no se firma (lector estricto de U-01).
    with pytest.raises(ValueError, match=r"(?i)payload|carga|tipo"):
        world.service.sign(SigningPurpose.GATE, {"keys": [], "issued_at": "x"})


def test_a_service_that_did_not_start_signs_nothing() -> None:
    world = asyncio.run(bootstrapped_world())
    fresh = world.new_service()
    with pytest.raises(SigningNotReady):
        fresh.sign(SigningPurpose.CATALOG, {})
    with pytest.raises(SigningNotReady):
        fresh.sign_detached(SigningPurpose.LIVE_VIEW_TOKEN, b"x")
    with pytest.raises(SigningNotReady):
        fresh.public_keys(SigningPurpose.CATALOG)
    with pytest.raises(SigningNotReady):
        fresh.current_key_set_envelope()


def test_an_expired_active_key_does_not_sign() -> None:
    world = asyncio.run(bootstrapped_world())
    key = _active(world, SigningPurpose.CHECKPOINT)
    world.clock.set(key.valid_until - timedelta(milliseconds=1))
    world.service.sign(SigningPurpose.CHECKPOINT, {"a": 1})
    world.clock.set(key.valid_until)
    with pytest.raises(SigningKeyUnavailable):
        world.service.sign(SigningPurpose.CHECKPOINT, {"a": 1})
    with pytest.raises(SigningKeyUnavailable):
        world.service.sign_detached(SigningPurpose.LIVE_VIEW_TOKEN, b"x")


def test_public_keys_are_active_and_overlapping_and_checkpoint_keeps_retired() -> None:
    async def scenario() -> tuple[SigningWorld, dict[str, list[str]]]:
        world = await bootstrapped_world()
        context = provider_context()
        first = {p.value: _active(world, p).key_id for p in SigningPurpose}
        for purpose in SigningPurpose:
            await world.service.rotate(purpose, context=context)
        world.clock.advance(timedelta(days=31).total_seconds())
        await world.service.retire_expired()
        return world, {"first": list(first.values())}

    world, ids = asyncio.run(scenario())
    for purpose in NODE_PURPOSES:
        published = world.service.public_keys(purpose)
        assert [k.status.value for k in published] == ["active"]
        assert not set(ids["first"]) & {k.key_id for k in published}
    checkpoint_ids = {k.key_id for k in world.service.public_keys(SigningPurpose.CHECKPOINT)}
    assert len(checkpoint_ids) == 2
    assert checkpoint_ids & set(ids["first"])


# --- El material privado no sale del proceso --------------------------------------------------


def _secret_forms(raw: bytes) -> set[str]:
    return {
        raw.hex(),
        base64.b64encode(raw).decode("ascii"),
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        base64.b64encode(raw).decode("ascii").rstrip("="),
        repr(raw),
    }


def test_private_material_never_leaves_memory_and_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario() -> tuple[SigningWorld, list[Any]]:
        world = await bootstrapped_world()
        outputs: list[Any] = []
        for purpose in SigningPurpose:
            outputs.append(await world.service.rotate(purpose, context=provider_context()))
        outputs.append(world.service.sign(SigningPurpose.CHECKPOINT, {"n": 1}).to_json())
        outputs.append(world.service.current_key_set_envelope())
        return world, outputs

    world, outputs = asyncio.run(scenario())
    assert len(world.secrets.values) == 10
    visible = "\n".join(
        [
            repr(world.service),
            str(world.service),
            repr(world.store.keys),
            repr([p.record for p in world.store.publications]),
            json.dumps(world.events.rotated),
            json.dumps(world.events.published),
            repr(outputs),
            repr([world.service.public_keys(p) for p in SigningPurpose]),
            caplog.text,
        ]
    )
    for raw in world.secrets.values.values():
        assert len(raw) == 32
        for form in _secret_forms(raw):
            assert form not in visible
    # Las referencias son nombres del gestor, nunca material.
    for key in world.service.all_keys():
        assert key.private_key_ref.startswith("arn:aws:secretsmanager:")
        assert f"/signing/{key.purpose.value}/{key.key_id}" in key.private_key_ref


def test_repr_of_service_lists_only_key_ids() -> None:
    world = asyncio.run(bootstrapped_world())
    text = repr(world.service)
    assert text.startswith("SigningService(ready=True, keys=[")
    assert "Ed25519PrivateKey" not in text
    assert isinstance(world.service, SigningService)
