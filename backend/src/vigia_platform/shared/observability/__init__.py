"""Registro JSON, OpenTelemetry, salud y métricas (LC-NUC-31, C-PLA-38).

- ``logging``: formateador JSON con los campos de NFR-NUC-17 y redacción; ``get_logger``.
- ``metrics``: nombres fijos de NFR-NUC-42 y una métrica por condición de NFR-NUC-38.
- ``tracing``: proveedores de OpenTelemetry con exportación OTLP de cola acotada y
  ``otel_dropped_total``; instrumentación automática activable desde la fábrica.
- ``redaction``: política única de atributos (identificadores y enumeraciones) y de texto.

``logging``, ``metrics`` y ``redaction`` no importan FastAPI ni SQLAlchemy: los módulos
críticos pueden registrar y medir sin romper su aislamiento (NFR-NUC-25).
"""
