"""Configuración del entorno efímero ``staging-<n>`` (infrastructure-design §2.1, D-8).

``staging`` es solo efímero: lo crea el flujo de release y lo destruye al terminar, también
en fallo. Columna ``staging-<n>`` de la tabla de D-8 (nota fechada de 2026-09-23 en §2.1):

- Depósitos ``vigia-evidence``, ``vigia-archive`` y ``vigia-edge``: ``vigia-<uso>-staging-
  <n>-<cuenta>-us-east-1``; sin bloqueo de objetos; ``DESTROY`` con vaciado automático.
- Registro de acceso: ``vigia-logs-staging-<n>-<cuenta>-us-east-1`` propio, creado por
  ``vigia-data``, con ``DESTROY`` y vaciado automático.
- Base de datos: ``db.t4g.small``; sin protección contra borrado; copias de 1 día; sin
  instantánea final.
- ``vigia-node-ca``: clave KMS de la ejecución, destruida con el entorno (borrado programado
  de 7 días).
- Zona DNS: importada por atributos (parámetros SSM de ``pilot``), no creada.
- Cortafuegos ``vigia-app-waf``: no.
- Pila ``vigia-datasets``: excluida; no se sintetiza ni se despliega.

Las tareas mínimas son las de ``pilot`` (§10: 3 vCPU más en ``staging``: 2 de 0,5 y 1 de 1).
"""

from __future__ import annotations

from config import pilot
from config.environment import SHARED_INSTANCE, EnvironmentConfig, NodesTlsMode

# §6.1 y nota D-8.
DB_INSTANCE_CLASS = "db.t4g.small"
DB_BACKUP_RETENTION_DAYS = 1
# §11 y nota D-8: borrado programado mínimo de KMS.
NODE_CA_PENDING_WINDOW_DAYS = 7
# D-8: la zona la aloja pilot y staging la importa por sus parámetros SSM (en una
# instancia dedicada, los de su propio despliegue permanente).
HOSTED_ZONE_SOURCE = "pilot"


def staging_config(
    environment: str,
    *,
    instance: str = SHARED_INSTANCE,
    first_deploy: bool = False,
    ca_rotation: bool = False,
    nat_per_az: bool = False,
    nodes_tls_mode: NodesTlsMode = NodesTlsMode.MTLS,
) -> EnvironmentConfig:
    """Configuración de ``staging-<n>``; ``environment`` ya viene validado."""
    return EnvironmentConfig(
        environment=environment,
        instance=instance,
        first_deploy=first_deploy,
        ca_rotation=ca_rotation,
        ephemeral=True,
        evidence_object_lock=None,
        archive_object_lock=None,
        buckets_retained=False,
        buckets_auto_delete_objects=True,
        access_logs_bucket_owned=True,
        db_instance_class=DB_INSTANCE_CLASS,
        db_multi_az=False,
        db_deletion_protection=False,
        db_final_snapshot=False,
        db_backup_retention_days=DB_BACKUP_RETENTION_DAYS,
        node_ca_per_run=True,
        node_ca_pending_window_days=NODE_CA_PENDING_WINDOW_DAYS,
        hosted_zone_owned=False,
        hosted_zone_source=HOSTED_ZONE_SOURCE if instance == SHARED_INSTANCE else instance,
        waf_enabled=False,
        include_datasets=False,
        api_min_tasks=pilot.API_MIN_TASKS,
        api_max_tasks=pilot.API_MAX_TASKS,
        worker_min_tasks=pilot.WORKER_MIN_TASKS,
        worker_max_tasks=pilot.WORKER_MAX_TASKS,
        nat_per_az=nat_per_az,
        nodes_tls_mode=nodes_tls_mode,
    )
