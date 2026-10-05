"""Manejadores de negocio de las rutas del contrato (TASK-219, 221, 222, 223, 226).

Cada módulo construye la ``NodeOperation`` de su ruta con los servicios que le da la raíz de
composición (``shared.runtime.units._node_operations``); la verificación previa común, la
traducción de los rechazos y la observabilidad son de ``node_api.router``.
"""
