"""Raíz de composición de producción de ``vigia-api``, ``vigia-worker`` y ``vigia-admin`` (A-52).

Los tres procesos piden sus dependencias al constructor que nombra su variable:

- ``VIGIA_API_RUNTIME=vigia_platform.shared.runtime.api:build_api_runtime``;
- ``VIGIA_WORKER_RUNTIME=vigia_platform.shared.runtime.worker:build_worker_runtime``;
- ``VIGIA_ADMIN_RUNTIME=vigia_platform.shared.runtime.admin:build_admin_runtime``.

Módulos: ``config`` (variables de entorno, ``RuntimeConfig``), ``db_credentials`` (credencial de
``vigia_app`` desde Secrets Manager y su relectura tras una rotación), ``units`` (el registro por
unidad), ``core`` (infraestructura común) y un constructor por proceso. Este paquete no importa
nada al cargarse: cada constructor trae lo suyo.
"""
