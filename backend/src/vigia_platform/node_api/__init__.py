"""``node_api``: el adaptador único de las rutas del contrato (LC-GOB-19; TASK-206; A-51).

Implementa el esqueleto que genera U-01 desde ``openapi/ingest.yaml``
(``vigia_contracts.server_skeleton``: ``create_router``, ``IngestOperations``, ``OPERATIONS``)
**sin modificarlo**, montado bajo ``/api/nodes``, y contiene todo lo común a las diez rutas:

- ``declarations``: lo que fija cada ``NodeRoute`` (operación de ``OPERATIONS``, lector estricto
  del cuerpo, codificaciones y parámetro de ruta);
- ``router``: el enrutador del esqueleto con la declaración ``node_route`` por operación y la
  verificación previa común (``NodeApiGate``), en el orden fijo de BR-GOB-84;
- ``identity`` y ``certificate_profile``: la identidad del nodo por petición, sin caché, desde
  las cabeceras del balanceador, y el perfil del sujeto del certificado;
- ``limits``: los límites de tasa por nodo, por origen (alta) y el freno global de emergencia;
- ``versioning``: la política de versiones con la función de compatibilidad de U-01;
- ``rejections``: la **traducción única** de cualquier fallo a ``rejection_code`` con el estado
  HTTP de A-37;
- ``observability``: métricas por ruta (NFR-GOB-54), tramos (NFR-GOB-57) y registro (NFR-GOB-25).

Ningún dominio importa de ``node_api``: las rutas de negocio (TASK-219, 221, 222, 223, 226)
registran aquí su manejador y solo implementan su operación.
"""
