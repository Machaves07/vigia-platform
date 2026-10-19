"""Interfaz HTTP de ``notifications`` para U-05 (``/notifications/*``, LC-LAZ §6.9).

- ``routes``: ``GET /notifications`` (``notifications.read``, propias; SUP-U05-33 en su forma
  mínima, TASK-324; TASK-348 la completa con el marcado de leída y las preferencias).

Los enrutadores no reciben dependencias al construirse (la especificación se exporta sin red,
NFR-NUC-52): en cada petición toman los servicios de ``NotificationsHttp`` en ``app.state``, que
la unidad ``notifications`` deja con ``API_STATE`` (A-52). Sin ellos, ``internal_error``.
"""
