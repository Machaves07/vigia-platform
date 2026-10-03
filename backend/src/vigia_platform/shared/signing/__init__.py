"""Servicio de firma Ed25519, claves y conjuntos publicados (LC-NUC-25).

Módulo crítico aislado (NFR-NUC-25): no importa FastAPI ni SQLAlchemy.

- ``keys``: dominio puro (propósitos, estados, transiciones de rotación, aviso y verificación).
- ``service``: ``SigningService`` (``SigningPort``) con sus puertos ``SigningKeyStore`` y
  ``KeyEventWriter``.
"""

from vigia_platform.shared.signing.keys import (
    AUTO_ROTATION_BEFORE,
    KEY_LIFETIME,
    NODE_PURPOSES,
    OVERLAP,
    ROTATION_NOTICE_BEFORE,
    KeySetPublicationRecord,
    KeyStateConflict,
    KeyStatus,
    KeyTransition,
    PlatformSignedEnvelope,
    ReminderDecision,
    SigningKeyRecord,
    SigningPurpose,
    verify_detached,
    verify_platform_envelope,
)
from vigia_platform.shared.signing.service import (
    REFRESH_INTERVAL_SECONDS,
    DetachedSignature,
    KeyEventWriter,
    KeyStoreSnapshot,
    RotationCommit,
    RotationRecorder,
    RotationResult,
    SigningKeyStore,
    SigningKeyUnavailable,
    SigningNotReady,
    SigningService,
    SigningStartupError,
    SigningStateError,
    secret_name,
)

__all__ = [
    "AUTO_ROTATION_BEFORE",
    "KEY_LIFETIME",
    "NODE_PURPOSES",
    "OVERLAP",
    "REFRESH_INTERVAL_SECONDS",
    "ROTATION_NOTICE_BEFORE",
    "DetachedSignature",
    "KeyEventWriter",
    "KeySetPublicationRecord",
    "KeyStateConflict",
    "KeyStatus",
    "KeyStoreSnapshot",
    "KeyTransition",
    "PlatformSignedEnvelope",
    "ReminderDecision",
    "RotationCommit",
    "RotationRecorder",
    "RotationResult",
    "SigningKeyRecord",
    "SigningKeyStore",
    "SigningKeyUnavailable",
    "SigningNotReady",
    "SigningPurpose",
    "SigningService",
    "SigningStartupError",
    "SigningStateError",
    "secret_name",
    "verify_detached",
    "verify_platform_envelope",
]
