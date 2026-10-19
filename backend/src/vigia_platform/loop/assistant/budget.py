"""Presupuesto del asistente: que una solicitud que no cabe no gaste nada (LC-LAZ-15;
PAT-LAZ-ESC-01 y 02; NFR-LAZ-25 y 35; PR-LAZ-52; TASK-316).

**Límites exactos** (PAT-LAZ-ESC-02), contados sobre ``loop.assistant_request.requested_at``
dentro de la **transacción de admisión**, con su índice ``(organization_id, user_id,
requested_at)``: **20 por hora y usuario** y **200 por día y organización** (ventanas deslizantes
``(ahora - 1 h, ahora]`` y ``(ahora - 24 h, ahora]``). ``admit`` toma primero el candado
consultivo de la organización (``loop.assistant_request_rate|<organización>``): excluye a la vez
la fila del usuario y el resto de su organización, así que dos admisiones concurrentes desde
conexiones distintas se cuentan una detrás de otra y ninguna ventana pasa de su límite. El
contador y la huella son **la misma fila**: no hay tabla contadora. Si no cabe,
``AssistantRateLimited`` con la espera hasta que la solicitud más antigua salga de su ventana.

**Tope mensual de fichas por organización** (NFR-LAZ-35): 1 000 000 de entrada y 200 000 de
salida ``[estimación propia]``, sumados en PostgreSQL desde las columnas de uso de las solicitudes
del mes UTC en curso (``laz_0033``). Al **80 %**, alarma
(``assistant_token_budget_used_ratio``, TASK-349); al **100 %** de cualquiera de los dos,
``AssistantBudgetExhausted``: la solicitud responde ``unavailable`` con ``budget_exceeded``
**antes de construir el contexto** (PAT-LAZ-ESC-01): ni fichas, ni espera, ni hueco del mamparo.

**Orden de candados** (uno solo): el candado de admisión de la organización. Ninguna otra
operación de U-04 lo toma; el consumidor no lo necesita (solo lee el presupuesto).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import text

from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics
from vigia_platform.shared.api.errors import MAX_RETRY_AFTER_SECONDS
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction

__all__ = [
    "ALARM_RATIO",
    "MONTHLY_INPUT_TOKENS",
    "MONTHLY_OUTPUT_TOKENS",
    "ORGANIZATION_WINDOW",
    "RATE_LOCK_PREFIX",
    "REQUESTS_PER_ORGANIZATION_PER_DAY",
    "REQUESTS_PER_USER_PER_HOUR",
    "USER_WINDOW",
    "AssistantBudgetExhausted",
    "AssistantRateLimited",
    "TokenBudget",
    "admit",
    "month_start",
    "report_budget",
    "token_budget",
]

REQUESTS_PER_USER_PER_HOUR: Final = 20
REQUESTS_PER_ORGANIZATION_PER_DAY: Final = 200
USER_WINDOW: Final = timedelta(hours=1)
ORGANIZATION_WINDOW: Final = timedelta(days=1)
MONTHLY_INPUT_TOKENS: Final = 1_000_000
"""Tope mensual de fichas de entrada por organización `[estimación propia]` (NFR-LAZ-35)."""
MONTHLY_OUTPUT_TOKENS: Final = 200_000
"""Tope mensual de fichas de salida por organización `[estimación propia]` (NFR-LAZ-35)."""
ALARM_RATIO: Final = 0.8
"""Fracción del tope a la que salta la alarma (NFR-LAZ-35)."""
RATE_LOCK_PREFIX: Final = "loop.assistant_request_rate|"
"""Clave del candado consultivo de admisión, seguida de la organización."""

_RATE_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_RATE_COUNT: Final = text(
    "SELECT count(*) FILTER (WHERE user_id = :user_id AND requested_at > :user_since) AS by_user,"
    " count(*) AS by_organization,"
    " min(requested_at) FILTER (WHERE user_id = :user_id AND requested_at > :user_since)"
    " AS oldest_by_user, min(requested_at) AS oldest_by_organization"
    " FROM loop.assistant_request"
    " WHERE organization_id = :organization_id AND requested_at > :organization_since"
)
_TOKENS: Final = text(
    "SELECT COALESCE(sum(input_tokens), 0) AS input_used,"
    " COALESCE(sum(output_tokens), 0) AS output_used FROM loop.assistant_request"
    " WHERE organization_id = :organization_id AND requested_at >= :month_start"
)


class AssistantRateLimited(Exception):
    """Se superaría uno de los dos límites exactos: ``rate_limited`` con su espera."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"límite del asistente; reintentar en {retry_after_seconds} s")


class AssistantBudgetExhausted(Exception):
    """El tope mensual de fichas de la organización está agotado (``budget_exceeded``)."""


@dataclass(frozen=True, slots=True)
class TokenBudget:
    """Fichas consumidas en el mes UTC en curso por la organización."""

    input_used: int
    output_used: int

    @property
    def used_ratio(self) -> float:
        """La mayor de las dos fracciones del tope (la que corta primero)."""
        return max(self.input_used / MONTHLY_INPUT_TOKENS, self.output_used / MONTHLY_OUTPUT_TOKENS)

    @property
    def exhausted(self) -> bool:
        return self.input_used >= MONTHLY_INPUT_TOKENS or self.output_used >= MONTHLY_OUTPUT_TOKENS

    @property
    def alarming(self) -> bool:
        return self.used_ratio >= ALARM_RATIO


def month_start(now: datetime) -> datetime:
    """El inicio del mes UTC de ``now``."""
    moment = now.astimezone(UTC)
    return datetime(moment.year, moment.month, 1, tzinfo=UTC)


async def token_budget(
    transaction: Transaction, organization_id: uuid.UUID, now: datetime
) -> TokenBudget:
    """Lo consumido en el mes de ``now``, sumado en la base desde el uso de cada solicitud."""
    row = (
        await transaction.execute(
            _TOKENS, {"organization_id": organization_id, "month_start": month_start(now)}
        )
    ).one()
    return TokenBudget(input_used=int(row.input_used), output_used=int(row.output_used))


async def admit(transaction: Transaction, context: ScopeContext, now: datetime) -> TokenBudget:
    """La admisión de una solicitud nueva, **antes** de construir el contexto.

    Raises:
        AssistantRateLimited: 20 en la última hora del usuario o 200 en el último día de la
            organización.
        AssistantBudgetExhausted: el tope mensual de fichas de la organización está agotado.
    """
    organization = context.organization_id
    await transaction.execute(_RATE_LOCK, {"key": f"{RATE_LOCK_PREFIX}{organization}"})
    row = (
        await transaction.execute(
            _RATE_COUNT,
            {
                "organization_id": organization,
                "user_id": context.actor.id,
                "user_since": now - USER_WINDOW,
                "organization_since": now - ORGANIZATION_WINDOW,
            },
        )
    ).one()
    waits: list[float] = []
    if int(row.by_user) >= REQUESTS_PER_USER_PER_HOUR:
        waits.append((row.oldest_by_user + USER_WINDOW - now).total_seconds())
    if int(row.by_organization) >= REQUESTS_PER_ORGANIZATION_PER_DAY:
        waits.append((row.oldest_by_organization + ORGANIZATION_WINDOW - now).total_seconds())
    if waits:
        # Cabe otra cuando la más antigua de cada ventana llena salga de ella; nunca hay más de
        # las permitidas, así que basta con la más antigua.
        wait = max(waits)
        raise AssistantRateLimited(min(MAX_RETRY_AFTER_SECONDS, max(1, math.ceil(wait))))
    budget = await token_budget(transaction, organization, now)
    if budget.exhausted:
        raise AssistantBudgetExhausted()
    return budget


def report_budget(metrics: LoopMetrics, organization_id: uuid.UUID, budget: TokenBudget) -> None:
    """La métrica de la alarma del 80 % (``assistant_token_budget_used_ratio``)."""
    metrics.gauge(LoopMetricName.ASSISTANT_TOKEN_BUDGET_USED_RATIO).set(
        round(budget.used_ratio, 6), {"organization_id": str(organization_id)}
    )
