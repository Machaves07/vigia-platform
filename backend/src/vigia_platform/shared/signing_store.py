"""``SqlSigningKeyStore``: el ``SigningKeyStore`` sobre PostgreSQL (LC-NUC-25, LC-NUC-29).

``shared.signing`` es un módulo crítico aislado (NFR-NUC-25) y no conoce SQLAlchemy; este
adaptador persiste sus claves en ``identity.signing_key`` y sus publicaciones en
``identity.key_set_publication`` (⛓ solo anexar), siempre con el contexto de la organización
proveedora (la seguridad a nivel de fila no deja ver ni escribir las de otra).

Garantías de ``commit_rotation`` y ``commit_transitions`` (``SigningKeyStore``):

- **Todo o nada**: clave nueva, transiciones y publicación van en una sola transacción.
- **Un solo confirmador a la vez**: cada confirmación toma primero el candado de transacción
  ``pg_advisory_xact_lock`` del almacén. Las confirmaciones de dos procesos (API, worker o
  ``vigia-admin``) se ordenan y cada una ve lo que confirmó la anterior.
- **Nada desde un estado superado**: cada ``KeyTransition`` es un ``UPDATE … WHERE key_id = :id
  AND status = :expected_status`` que debe afectar exactamente una fila; y una rotación que
  publica conjunto solo se confirma si la última publicación sigue siendo
  ``expected_publication_id`` (sonda R3 de VIG-60: dos rotaciones de propósitos distintos en dos
  procesos no publican un conjunto sin la clave de la otra ni repiten ``issued_at``). Si no,
  ``KeyStateConflict`` y no queda nada; el servicio relee y el llamador puede reintentar.
- Una violación de los índices únicos de ``signing_key`` (una ``active`` y una ``overlapping``
  por propósito) también es ``KeyStateConflict``: dos altas iniciales del mismo propósito a la
  vez.

Sin material privado: la base guarda la clave pública y la referencia al secreto.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from vigia_contracts.models.public_key_set import SignedEnvelope as SignedPublicKeySet

from vigia_platform.ledger.application.writer import violated_constraint
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import (
    KeySetPublicationRecord,
    KeyStateConflict,
    KeyStatus,
    KeyTransition,
    SigningKeyRecord,
    SigningPurpose,
)
from vigia_platform.shared.signing.service import KeyStoreSnapshot, RotationCommit

__all__ = ["STORE_LOCK_KEY", "SqlSigningKeyStore"]

STORE_LOCK_KEY: Final = 0x76696769615F6B73
"""Clave del candado de transacción del almacén (``vigia_ks`` en ASCII)."""

_UNIQUE_VIOLATION: Final = "23505"
_UNIQUE_CONSTRAINTS: Final = frozenset(
    {
        "signing_key_pkey",
        "signing_key_one_active_per_purpose",
        "signing_key_one_overlapping_per_purpose",
    }
)

_LOCK: Final = text("SELECT pg_advisory_xact_lock(:lock_key)")
_KEYS: Final = text(
    "SELECT key_id, purpose, public_key, private_key_ref, valid_from, valid_until, status,"
    " created_at, rotated_by FROM identity.signing_key ORDER BY key_id"
)
_LATEST_PUBLICATION: Final = text(
    "SELECT publication_id, issued_at, keys, signed_by_key_id, envelope"
    " FROM identity.key_set_publication ORDER BY issued_at DESC, publication_id DESC LIMIT 1"
)
_LATEST_PUBLICATION_ID: Final = text(
    "SELECT publication_id FROM identity.key_set_publication"
    " ORDER BY issued_at DESC, publication_id DESC LIMIT 1"
)
_TRANSITION: Final = text(
    "UPDATE identity.signing_key SET status = :status, valid_until = :valid_until"
    " WHERE key_id = :key_id AND status = :expected_status RETURNING key_id"
)
_INSERT_KEY: Final = text(
    "INSERT INTO identity.signing_key (key_id, organization_id, purpose, algorithm, public_key,"
    " private_key_ref, valid_from, valid_until, status, created_at, rotated_by)"
    " VALUES (:key_id, :organization_id, :purpose, 'Ed25519', :public_key, :private_key_ref,"
    " :valid_from, :valid_until, :status, :created_at, :rotated_by)"
)
_INSERT_PUBLICATION: Final = text(
    "INSERT INTO identity.key_set_publication (publication_id, organization_id, issued_at, keys,"
    " signed_by_key_id, envelope) VALUES (:publication_id, :organization_id, :issued_at,"
    " CAST(:keys AS jsonb), :signed_by_key_id, CAST(:envelope AS jsonb))"
)


class StoreDatabase(Protocol):
    """``shared.db.Database``."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


def _as_uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _json_value(value: object) -> Any:
    """``jsonb`` llega ya decodificado con asyncpg; como texto, se decodifica aquí."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _key(row: Any) -> SigningKeyRecord:
    valid_until = row.valid_until
    if not isinstance(valid_until, datetime):
        raise ValueError("una clave de firma sin valid_until no la escribe la plataforma")
    return SigningKeyRecord(
        key_id=row.key_id,
        purpose=SigningPurpose(row.purpose),
        public_key=row.public_key,
        private_key_ref=row.private_key_ref,
        valid_from=row.valid_from,
        valid_until=valid_until,
        status=KeyStatus(row.status),
        created_at=row.created_at,
        rotated_by=_as_uuid(row.rotated_by),
    )


def _publication(row: Any) -> KeySetPublicationRecord:
    keys = _json_value(row.keys)
    if not isinstance(keys, list) or not all(isinstance(key, Mapping) for key in keys):
        raise ValueError("key_set_publication.keys no es una lista de claves")
    envelope = SignedPublicKeySet.model_validate_json(json.dumps(_json_value(row.envelope)))
    return KeySetPublicationRecord(
        publication_id=_as_uuid(row.publication_id),
        issued_at=row.issued_at,
        keys=tuple({str(name): str(value) for name, value in key.items()} for key in keys),
        signed_by_key_id=row.signed_by_key_id,
        envelope=envelope,
    )


def _envelope_json(envelope: SignedPublicKeySet) -> str:
    return json.dumps(envelope.model_dump(mode="json", by_alias=True, exclude_none=True))


class SqlSigningKeyStore:
    """``SigningKeyStore`` de ``shared.signing`` sobre ``identity.signing_key``."""

    def __init__(self, *, database: StoreDatabase, context: Callable[[], ScopeContext]) -> None:
        """``context`` da el contexto de la organización proveedora para cada transacción
        (``ScopeContexts.provider_audit_context`` o el de la orden administrativa)."""
        self._database = database
        self._context = context

    def __repr__(self) -> str:
        return "SqlSigningKeyStore()"

    async def load(self) -> KeyStoreSnapshot:
        async with self._database.transaction(self._context()) as transaction:
            keys = (await transaction.execute(_KEYS)).all()
            latest = (await transaction.execute(_LATEST_PUBLICATION)).first()
        return KeyStoreSnapshot(
            keys=tuple(_key(row) for row in keys),
            publication=None if latest is None else _publication(latest),
        )

    async def commit_rotation(self, commit: RotationCommit) -> None:
        context = self._context()
        new_key = commit.new_key
        try:
            async with self._database.transaction(context) as transaction:
                await transaction.execute(_LOCK, {"lock_key": STORE_LOCK_KEY})
                if commit.publication is not None:
                    latest = (await transaction.execute(_LATEST_PUBLICATION_ID)).first()
                    latest_id = None if latest is None else _as_uuid(latest.publication_id)
                    if latest_id != commit.expected_publication_id:
                        raise KeyStateConflict
                await _apply(transaction, commit.transitions)
                await transaction.execute(
                    _INSERT_KEY,
                    {
                        "key_id": new_key.key_id,
                        "organization_id": context.organization_id,
                        "purpose": new_key.purpose.value,
                        "public_key": new_key.public_key,
                        "private_key_ref": new_key.private_key_ref,
                        "valid_from": new_key.valid_from,
                        "valid_until": new_key.valid_until,
                        "status": new_key.status.value,
                        "created_at": new_key.created_at,
                        "rotated_by": new_key.rotated_by,
                    },
                )
                publication = commit.publication
                if publication is not None:
                    await transaction.execute(
                        _INSERT_PUBLICATION,
                        {
                            "publication_id": publication.publication_id,
                            "organization_id": context.organization_id,
                            "issued_at": publication.issued_at,
                            "keys": json.dumps([dict(key) for key in publication.keys]),
                            "signed_by_key_id": publication.signed_by_key_id,
                            "envelope": _envelope_json(publication.envelope),
                        },
                    )
        except sa_exc.IntegrityError as error:
            if violated_constraint(error, _UNIQUE_VIOLATION) in _UNIQUE_CONSTRAINTS:
                raise KeyStateConflict from None
            raise

    async def commit_transitions(self, transitions: Sequence[KeyTransition]) -> None:
        async with self._database.transaction(self._context()) as transaction:
            await transaction.execute(_LOCK, {"lock_key": STORE_LOCK_KEY})
            await _apply(transaction, transitions)


async def _apply(transaction: Transaction, transitions: Sequence[KeyTransition]) -> None:
    """Cada transición solo si la clave sigue en ``expected_status``; si no, revierte todo."""
    for transition in transitions:
        updated = (
            await transaction.execute(
                _TRANSITION,
                {
                    "key_id": transition.key_id,
                    "status": transition.status.value,
                    "valid_until": transition.valid_until,
                    "expected_status": transition.expected_status.value,
                },
            )
        ).all()
        if len(updated) != 1:
            raise KeyStateConflict
