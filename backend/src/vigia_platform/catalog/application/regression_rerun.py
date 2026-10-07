"""``catalog.regression``: apertura de la reejecución por regresión (LC-GOB-09; BR-GOB-55).

``POST /zones/{zone_id}/walk-tests/regression-rerun`` (``commissioning.run`` sobre la zona), con
``{passes_per_cell (≥ 3)}``:

1. la regresión de la zona tiene que estar ``pending`` (``catalog.walk_test_regression``); si no,
   ``conflict`` sin ``detail_code``;
2. después, las **mismas** guardas de apertura que la sesión ``initial`` (TASK-214, en
   ``WalkTestService.open_planned``): montaje ``approved``, nodo asignado, ninguna sesión abierta
   en la zona y ``passes_per_cell >= 3``;
3. la sesión nace ``kind = regression_rerun`` con **solo** las filas afectadas de la matriz que
   ``derive_matrix`` deriva del catálogo vigente, o con la matriz completa si la causa es ``all``
   (``commissioning_record.rerun_rows``), y guarda la última marca de la regresión
   (``regression_basis_record_id``): el cierre del acta solo devuelve la zona a ``current`` si
   ninguna marca llegó después (TASK-216).

La regresión se vuelve a leer dentro de la transacción de la apertura: una marca confirmada entre
la primera lectura y la apertura queda como base o, si la regresión ya no está ``pending``,
``conflict``. Nada toca las compuertas ni ``resulting_mode``: la zona sigue ``productive``
(BR-GOB-53). Ningún candado nuevo: la apertura la decide el índice único de la sesión abierta.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.walk_test import (
    SessionPlan,
    WalkTestConflict,
    WalkTestService,
)
from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.commissioning_record import rerun_rows
from vigia_platform.catalog.domain.enums import WalkTestKind
from vigia_platform.catalog.domain.regression import WalkTestRegression
from vigia_platform.catalog.domain.walk_test import WalkTestSession
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["RegressionRerunService"]


def _pending(regression: WalkTestRegression | None) -> WalkTestRegression:
    if regression is None or not regression.pending or regression.ledger_record_id is None:
        raise WalkTestConflict
    return regression


@repository
class RegressionRerunService:
    """La reejecución por regresión: una sesión de walk-test con las filas afectadas."""

    def __init__(
        self,
        *,
        walk_tests: WalkTestService,
        regressions: PostgresRegressionRepository,
        database: LedgerDatabase,
    ) -> None:
        self._walk_tests = walk_tests
        self._regressions = regressions
        self._database = database

    def __repr__(self) -> str:
        return "RegressionRerunService()"

    async def open(
        self, context: ScopeContext, zone_id: uuid.UUID, passes_per_cell: object
    ) -> WalkTestSession:
        """Abre la reejecución de la zona; la devuelve tras confirmar.

        ``ResourceNotFound`` (zona inexistente o fuera de alcance), ``WalkTestConflict`` (la
        regresión no está ``pending``) y los errores de ``WalkTestService.open``; en todos, nada
        queda escrito.
        """

        async def before(zone: ZoneRef, authorized: ScopeContext) -> None:
            async with self._database.transaction(authorized) as transaction:
                _pending(await self._regressions.get(transaction, zone.zone_id))

        async def plan(
            transaction: Transaction, zone: ZoneRef, catalog: Mapping[str, Any], passes: int
        ) -> SessionPlan:
            regression = _pending(await self._regressions.get(transaction, zone.zone_id))
            return SessionPlan(
                kind=WalkTestKind.REGRESSION_RERUN,
                rows=rerun_rows(regression, catalog, passes),
                regression_basis_record_id=regression.ledger_record_id,
            )

        return await self._walk_tests.open_planned(
            context, zone_id, passes_per_cell, plan, before=before
        )
