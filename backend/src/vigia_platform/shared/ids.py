"""Identificadores UUID v7 de la plataforma (domain-entities, convenciones).

``record_id``, ``evidence_id``, ``label_id`` y ``entry_id`` son UUID v7 (RFC 9562): 48 bits con
los milisegundos del ``Clock`` inyectado y 74 bits aleatorios. El módulo no lee la hora del
sistema (PAT-NUC-RES-07): la recibe del reloj.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from vigia_platform.shared.clock import Clock

__all__ = ["uuid7", "uuid7_at"]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def uuid7_at(moment: datetime, random: bytes) -> uuid.UUID:
    """UUID v7 con los milisegundos de ``moment`` (con zona) y 10 bytes aleatorios."""
    if moment.tzinfo is None:
        raise ValueError("la marca del UUID v7 debe llevar zona horaria")
    if len(random) < 10:
        raise ValueError("un UUID v7 necesita 10 bytes aleatorios")
    milliseconds = (moment.astimezone(UTC) - _EPOCH) // timedelta(milliseconds=1)
    rand_a = int.from_bytes(random[:2], "big") & 0x0FFF
    rand_b = int.from_bytes(random[2:10], "big") & ((1 << 62) - 1)
    value = (milliseconds & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def uuid7(clock: Clock, random_bytes: Callable[[int], bytes] = os.urandom) -> uuid.UUID:
    """UUID v7 con la hora de ``clock``."""
    return uuid7_at(clock.now(), random_bytes(10))
