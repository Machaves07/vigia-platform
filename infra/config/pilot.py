"""Configuración del entorno permanente ``pilot`` (infrastructure-design §2.1).

Columna ``pilot`` de la tabla de D-8 (nota fechada de 2026-09-23 en §2.1):

- Depósitos ``vigia-evidence``, ``vigia-archive`` y ``vigia-edge``: ``vigia-<uso>-<cuenta>-
  us-east-1``; bloqueo de objetos (gobernanza de 365 días en evidencias, cumplimiento de 10
  años en archivos); ``RETAIN``.
- Base de datos: ``db.t4g.medium`` en dos zonas; eliminación protegida; ``RETAIN`` con
  instantánea final; copias de 35 días.
- ``vigia-node-ca``: clave KMS permanente.
- Zona DNS: ``vigia-zone``, alojada en la pila ``vigia-edge``.
- Cortafuegos ``vigia-app-waf``: sí.
- Pila ``vigia-datasets``: única en la cuenta, sin sufijo de entorno.

Una instancia dedicada (``instance=<cliente>``, deployment-architecture §7) usa la misma
configuración con sus propios nombres y sin ``vigia-datasets``, que custodia el conjunto
sellado de U-01 en la cuenta compartida; por eso crea su propio depósito de registros de
acceso, como ``staging``.
"""

from __future__ import annotations

from config.environment import (
    PILOT,
    SHARED_INSTANCE,
    EnvironmentConfig,
    ObjectLock,
    ObjectLockMode,
)

# [objetivo propio] §6.2: gobernanza de 365 días en evidencias (revisar con R5) y
# cumplimiento de 10 años en archivos de auditoría.
EVIDENCE_LOCK_DAYS = 365
ARCHIVE_LOCK_DAYS = 3653  # 10 años, con los días bisiestos
# §6.1: clase de la base y copias automáticas de 35 días.
DB_INSTANCE_CLASS = "db.t4g.medium"
DB_BACKUP_RETENTION_DAYS = 35
# Ventana máxima de borrado programado de KMS; la clave de pilot se retiene igualmente.
NODE_CA_PENDING_WINDOW_DAYS = 30
# §5.2 y A-21: vigia-api de 2 a 6 tareas, vigia-worker de 1 a 3.
API_MIN_TASKS = 2
API_MAX_TASKS = 6
WORKER_MIN_TASKS = 1
WORKER_MAX_TASKS = 3


def pilot_config(
    *,
    instance: str = SHARED_INSTANCE,
    first_deploy: bool = False,
    ca_rotation: bool = False,
    nat_per_az: bool = False,
) -> EnvironmentConfig:
    """Configuración de ``pilot`` para la instancia compartida o una dedicada."""
    return EnvironmentConfig(
        environment=PILOT,
        instance=instance,
        first_deploy=first_deploy,
        ca_rotation=ca_rotation,
        ephemeral=False,
        evidence_object_lock=ObjectLock(ObjectLockMode.GOVERNANCE, EVIDENCE_LOCK_DAYS),
        archive_object_lock=ObjectLock(ObjectLockMode.COMPLIANCE, ARCHIVE_LOCK_DAYS),
        buckets_retained=True,
        buckets_auto_delete_objects=False,
        # Sin vigia-datasets, una instancia dedicada crea su propio depósito de registros.
        access_logs_bucket_owned=instance != SHARED_INSTANCE,
        db_instance_class=DB_INSTANCE_CLASS,
        db_multi_az=True,
        db_deletion_protection=True,
        db_final_snapshot=True,
        db_backup_retention_days=DB_BACKUP_RETENTION_DAYS,
        node_ca_per_run=False,
        node_ca_pending_window_days=NODE_CA_PENDING_WINDOW_DAYS,
        hosted_zone_owned=True,
        hosted_zone_source=None,
        waf_enabled=True,
        include_datasets=instance == SHARED_INSTANCE,
        api_min_tasks=API_MIN_TASKS,
        api_max_tasks=API_MAX_TASKS,
        worker_min_tasks=WORKER_MIN_TASKS,
        worker_max_tasks=WORKER_MAX_TASKS,
        nat_per_az=nat_per_az,
    )
