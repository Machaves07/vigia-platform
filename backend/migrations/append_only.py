"""Tablas de solo anexar (⛓) que el lint de migraciones protege (NFR-NUC-14, PAT-NUC-MAN-09).

Ninguna migración puede ejecutar ``DROP TABLE``, ``TRUNCATE`` ni ``DELETE`` sobre ellas, ni
``DROP SCHEMA`` sobre un esquema que las contenga (``tools/lint_migrations.py``, regla MIG002).
Una tabla cuenta también por sus particiones: ``shared.audit_entry`` protege
``shared.audit_entry_2026_10`` y ``fleet.heartbeat_history`` protege
``fleet.heartbeat_history_2026_10``.

- ``APPEND_ONLY_SCHEMAS``: esquemas enteros de solo anexar. ``ledger`` lo es entero (fallo
  cerrado: el expediente no se borra, P4; también ``ledger.record_type``, porque retirar un tipo
  está prohibido).
- ``APPEND_ONLY_TABLES``: tablas ⛓ fuera de esos esquemas, ``esquema.tabla`` en minúsculas, con
  los nombres de las entidades ⛓ de ``domain-entities.md`` §2 y §4. La migración que crea una
  tabla ⛓ con otro nombre la añade aquí en el mismo cambio (TASK-107, TASK-108; U-03 y U-04
  con las suyas).

Solo constantes: el lint carga este archivo sin importar nada más.
"""

from __future__ import annotations

from typing import Final

APPEND_ONLY_SCHEMAS: Final[frozenset[str]] = frozenset({"ledger"})

APPEND_ONLY_TABLES: Final[frozenset[str]] = frozenset(
    {
        # identity (TASK-107)
        "identity.zone_node_assignment",
        "identity.role_assignment",
        "identity.provider_concession",
        "identity.key_set_publication",
        "identity.live_view_token_issuance",
        "identity.privacy_notice_acceptance",
        # shared (TASK-108)
        "shared.audit_entry",
        "shared.outbox_event",
        "shared.dead_letter",
        # catalog (TASK-202, gob_0017)
        "catalog.zone_catalog_version",
        "catalog.declared_standard_version",
        "catalog.family_admission",
        "catalog.gate_state_history",
        "catalog.mounting_gate_record",
        "catalog.use_agreement",
        "catalog.agreement_confirmation",
        "catalog.plant_policy",
        "catalog.walk_test_step",
        "catalog.walk_test_pass",
        "catalog.occlusion_test",
        "catalog.commissioning_record",
        # fleet (TASK-203, gob_0018); las tres primeras, particionadas por mes
        "fleet.enrollment_attempt",
        "fleet.heartbeat_history",
        "fleet.fleet_alarm",
        "fleet.target_version_publication",
        "fleet.update_result",
        "fleet.verification_clip",
        # fleet (TASK-221, gob_0024): marca del cierre huérfano de un evento de observabilidad
        "fleet.observability_orphan_close",
    }
)
