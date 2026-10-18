"""Interfaz HTTP de ``accreditation`` para U-05 (``/accreditation/*``, LC-LAZ §6.7).

- ``export_routes``: ``POST /accreditation/exports`` y ``GET /accreditation/exports/{export_id}``
  (``export.create``, zona; SUP-U05-21 y 22, TASK-317).

Los enrutadores no reciben dependencias al construirse (la especificación se exporta sin red,
NFR-NUC-52): en cada petición toman los servicios de ``AccreditationHttp`` en ``app.state``, que
la unidad ``accreditation`` deja con ``API_STATE`` (A-52). Sin ellos, ``internal_error``.
"""
