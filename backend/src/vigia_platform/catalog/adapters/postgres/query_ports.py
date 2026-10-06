"""Punto único de construcción de los puertos de lectura del catálogo para U-04 (LC-GOB-23a).

``catalog_query_ports(database)`` devuelve ``CatalogQueryPort`` y ``GateQueryPort`` sobre
PostgreSQL con la base de la raíz de composición (``UnitServices.database``). La raíz de TASK-201
todavía no tiene un registro de puertos por unidad: U-04 los recibirá desde aquí cuando registre
su entrada en ``REGISTERED_UNITS``, sin que esta tarea toque la raíz.
"""

from __future__ import annotations

from dataclasses import dataclass

from vigia_platform.catalog.adapters.postgres.catalog_query import PostgresCatalogQuery
from vigia_platform.catalog.adapters.postgres.gate_query import PostgresGateQuery
from vigia_platform.catalog.domain.ports import CatalogQueryPort, GateQueryPort
from vigia_platform.ledger.application.writer import LedgerDatabase

__all__ = ["CatalogQueryPorts", "catalog_query_ports"]


@dataclass(frozen=True, slots=True)
class CatalogQueryPorts:
    """Los dos puertos de lectura del módulo ``catalog`` para U-04."""

    catalog: CatalogQueryPort
    gates: GateQueryPort


def catalog_query_ports(database: LedgerDatabase) -> CatalogQueryPorts:
    """Los puertos sobre ``database`` (sin estado propio: ni caché ni conexiones)."""
    return CatalogQueryPorts(
        catalog=PostgresCatalogQuery(database), gates=PostgresGateQuery(database)
    )
