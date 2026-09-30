"""Dobles de prueba del segundo factor (TASK-123): KMS en memoria que se puede «caer», KMS real
conmutable y un ``SecondFactorStore`` en memoria con la semántica del SQL del adaptador.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vigia_platform.identity.auth.second_factor import (
    AlreadyEnrolled,
    RecoveryCodeRecord,
    SecondFactorNotFound,
    TotpCredential,
)
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.secrets import DataKey, Dependency, KmsPort, SecretsUnavailable


def _binding(key_id: str, context: Mapping[str, str]) -> bytes:
    """Lo que KMS liga a una clave envuelta: la clave maestra y el contexto de cifrado."""
    return json.dumps([key_id, sorted(context.items())]).encode()


class FakeKms:
    """``KmsPort`` en memoria: envuelve las claves de datos con AES-GCM y una clave maestra.

    Como KMS, liga la clave envuelta a la clave maestra y al contexto de cifrado: descifrar con
    otros lanza ``ValueError``. ``down = True`` simula KMS inaccesible: toda llamada lanza
    ``SecretsUnavailable``.
    """

    def __init__(self) -> None:
        self._master = AESGCM(os.urandom(32))
        self.down = False
        self.generate_calls = 0
        self.decrypt_calls = 0

    async def generate_data_key(self, key_id: str, *, context: Mapping[str, str]) -> DataKey:
        self._check("generate_data_key")
        self.generate_calls += 1
        plaintext = os.urandom(32)
        nonce = os.urandom(12)
        wrapped = nonce + self._master.encrypt(nonce, plaintext, _binding(key_id, context))
        return DataKey(plaintext=plaintext, wrapped=wrapped, key_id=key_id)

    async def decrypt(self, wrapped: bytes, *, key_id: str, context: Mapping[str, str]) -> bytes:
        self._check("decrypt")
        self.decrypt_calls += 1
        try:
            return self._master.decrypt(wrapped[:12], wrapped[12:], _binding(key_id, context))
        except Exception:
            raise ValueError("la clave envuelta no corresponde") from None

    async def sign(self, key_id: str, message: bytes) -> bytes:
        raise NotImplementedError

    async def get_public_key(self, key_id: str) -> bytes:
        raise NotImplementedError

    def _check(self, operation: str) -> None:
        if self.down:
            raise SecretsUnavailable(Dependency.KMS, operation)


class SwitchableKms:
    """``KmsPort`` que delega en ``inner``: la prueba lo cambia en mitad de la operación por un
    ``KmsAdapter`` contra un puerto cerrado (KMS inaccesible de verdad, conexión rechazada)."""

    def __init__(self, inner: KmsPort) -> None:
        self.inner = inner

    async def generate_data_key(self, key_id: str, *, context: Mapping[str, str]) -> DataKey:
        return await self.inner.generate_data_key(key_id, context=context)

    async def decrypt(self, wrapped: bytes, *, key_id: str, context: Mapping[str, str]) -> bytes:
        return await self.inner.decrypt(wrapped, key_id=key_id, context=context)

    async def sign(self, key_id: str, message: bytes) -> bytes:
        return await self.inner.sign(key_id, message)

    async def get_public_key(self, key_id: str) -> bytes:
        return await self.inner.get_public_key(key_id)


@dataclass
class InMemorySecondFactorStore:
    """``SecondFactorStore`` con la semántica de ``PostgresSecondFactorStore``.

    Una organización por almacén (``organization_id``); ``users`` son los usuarios que existen.
    """

    organization_id: uuid.UUID
    users: set[uuid.UUID] = field(default_factory=set)
    credentials: dict[uuid.UUID, TotpCredential] = field(default_factory=dict)
    codes: dict[uuid.UUID, RecoveryCodeRecord] = field(default_factory=dict)
    enrolled_at: dict[uuid.UUID, datetime | None] = field(default_factory=dict)
    sessions: dict[uuid.UUID, int] = field(default_factory=dict)
    audit: list[tuple[str, uuid.UUID]] = field(default_factory=list)
    recovery_queries: int = 0
    """Llamadas a ``unused_recovery_codes`` (para ver que el servicio no consulta sin motivo)."""

    def _visible(self, context: ScopeContext, user_id: uuid.UUID) -> bool:
        return context.organization_id == self.organization_id and user_id in self.users

    async def get_credential(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> TotpCredential | None:
        if not self._visible(context, user_id):
            return None
        return self.credentials.get(user_id)

    async def save_enrollment(
        self,
        context: ScopeContext,
        credential: TotpCredential,
        recovery_codes: Sequence[RecoveryCodeRecord],
    ) -> None:
        if not self._visible(context, credential.user_id):
            raise SecondFactorNotFound()
        existing = self.credentials.get(credential.user_id)
        if existing is not None and existing.active:
            raise AlreadyEnrolled()
        self.credentials[credential.user_id] = replace(
            credential, last_accepted_step=None, disabled_at=None
        )
        for record in recovery_codes:
            self.codes[record.recovery_code_id] = record
        self.enrolled_at[credential.user_id] = credential.enrolled_at
        self.audit.append(("second_factor_enrolled", credential.user_id))

    async def advance_step(self, context: ScopeContext, user_id: uuid.UUID, step: int) -> bool:
        credential = await self.get_credential(context, user_id)
        if credential is None or not credential.active:
            return False
        if credential.last_accepted_step is not None and credential.last_accepted_step >= step:
            return False
        self.credentials[user_id] = replace(credential, last_accepted_step=step)
        return True

    def _current(self, record: RecoveryCodeRecord) -> bool:
        credential = self.credentials.get(record.user_id)
        return (
            credential is not None
            and credential.active
            and record.generated_at == credential.enrolled_at
        )

    async def unused_recovery_codes(
        self, context: ScopeContext, user_id: uuid.UUID
    ) -> Sequence[RecoveryCodeRecord]:
        self.recovery_queries += 1
        if not self._visible(context, user_id):
            return ()
        return tuple(
            record
            for record in sorted(self.codes.values(), key=lambda r: r.recovery_code_id)
            if record.user_id == user_id and record.used_at is None and self._current(record)
        )

    async def mark_recovery_code_used(
        self, context: ScopeContext, recovery_code_id: uuid.UUID, used_at: datetime
    ) -> bool:
        record = self.codes.get(recovery_code_id)
        if (
            record is None
            or not self._visible(context, record.user_id)
            or record.used_at is not None
            or not self._current(record)
        ):
            return False
        self.codes[recovery_code_id] = replace(record, used_at=used_at)
        return True

    async def reset(self, context: ScopeContext, user_id: uuid.UUID, now: datetime) -> int:
        if not self._visible(context, user_id):
            raise SecondFactorNotFound()
        self.enrolled_at[user_id] = None
        credential = self.credentials.get(user_id)
        if credential is not None and credential.active:
            self.credentials[user_id] = replace(credential, disabled_at=now)
        closed = self.sessions.pop(user_id, 0)
        self.audit.append(("second_factor_reset", user_id))
        return closed
