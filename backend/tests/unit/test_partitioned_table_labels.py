"""Etiquetas de ``default_partition_rows`` (TASK-339; PAT-NUC-ESC-01; NFR-LAZ-07).

Cada ``PartitionedTable`` publica la métrica con una etiqueta que la política de redacción admite
en su lista cerrada ``table``: si una sola no pasa, ``register`` rechaza la lista entera y todas
las tablas salen como ``other`` (la alarma pierde qué tabla tiene filas en la partición por
defecto). ``notifications.notification_delivery`` tiene una tira de 21 caracteres que la
redacción toma por un token: su etiqueta es ``notifications.delivery``.
"""

from __future__ import annotations

from tests.identity_db import BASE_TIME
from vigia_platform.shared.archive.partitions import PartitionedTable, PartitionMaintenance
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.redaction import AttributePolicy


def test_every_label_survives_redaction() -> None:
    rejected = [
        table.metric_label
        for table in PartitionedTable
        if redaction.redact_text(table.metric_label) != table.metric_label
    ]
    assert rejected == []
    # Las etiquetas no chocan: cada tabla se distingue en la métrica.
    assert len({table.metric_label for table in PartitionedTable}) == len(PartitionedTable)


def test_the_labels_register_in_a_fresh_policy() -> None:
    policy = AttributePolicy()
    policy.register("table", [table.metric_label for table in PartitionedTable])


def test_only_the_delivery_label_differs_from_the_table() -> None:
    assert {
        table: table.metric_label for table in PartitionedTable if table.metric_label != table.value
    } == {PartitionedTable.NOTIFICATION_DELIVERY: "notifications.delivery"}


def test_the_maintenance_registers_every_label() -> None:
    PartitionMaintenance(clock=SimulatedClock(BASE_TIME))
    for table in PartitionedTable:
        cleaned = redaction.DEFAULT_POLICY.clean_value("table", table.metric_label, "other")
        assert cleaned == table.metric_label, table
