"""``NodeConfiguration`` y la configuración inicial del alta (nota U03-H-12 de DE §3.14; D-11).

Configuración por planta y nodo con los valores por defecto de D-11 ``[objetivo propio]``:
latido de 60 s con ``mute_after_seconds = 5 * interval_seconds`` (A-04, pendiente nº 35: la
plataforma nunca emite una configuración que rompa la igualdad), ``sent_records_retention_days``
30, ``token_max_age_seconds`` 600 y ``time_sources`` de la planta. Si el nodo tiene fila en
``fleet.node_configuration`` manda la fila; si no, los valores por defecto.

``initial_configuration`` arma ``NodeInitialConfiguration`` del contrato (U-01 §3.4) con:

- ``zones`` y ``gate_states``: los **sobres ya almacenados** (catálogo vigente y
  ``SignedEnvelope<GateState>`` conservado de cada zona asignada), tal cual, sin firmar nada
  (NFR-GOB-10, PAT-GOB-REN-02);
- ``cameras``: las del catálogo vigente de cada zona (``camera_id`` y ``code``) cruzadas con su
  ``stream_reference`` de ``ZoneCamera`` (``catalog.zone_camera`` no borra filas: una cámara que
  salió del catálogo no viaja). Una misma cámara declarada en dos zonas del nodo (A-57) viaja una
  vez, con la ``stream_reference`` de la primera zona en el orden de asignación;
- ``gate_cache_ttl_seconds`` 604 800 (BR-CTR-43), ``credential`` (365, 30, 15) y
  ``endpoints.ingest_base_url`` de la configuración del proceso (``VIGIA_NODES_BASE_URL``).

Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from vigia_platform.fleet.domain.node_credential import (
    ALERT_BEFORE_DAYS,
    ROTATE_BEFORE_DAYS,
    VALIDITY_DAYS,
)

__all__ = [
    "DEFAULT_TIME_SOURCES",
    "GATE_CACHE_TTL_SECONDS",
    "MUTE_FACTOR",
    "BootstrapZone",
    "ConfigurationUnavailable",
    "NodeConfiguration",
    "StoredCamera",
    "initial_configuration",
]

MUTE_FACTOR: Final = 5
"""``mute_after_seconds = 5 * interval_seconds`` (A-04, A-09)."""
GATE_CACHE_TTL_SECONDS: Final = 604_800
"""Vigencia del caché de compuertas del nodo: 7 días (BR-CTR-43)."""
DEFAULT_TIME_SOURCES: Final = ("pool.ntp.org",)
"""Fuente de tiempo sin fila de configuración de la planta ``[objetivo propio]``: el nodo mide
además contra ``server_time`` del latido (BR-BOR-61)."""
_TIME_SOURCE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_STREAM_REFERENCE: Final = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class ConfigurationUnavailable(Exception):
    """No se puede armar la configuración inicial ahora (zona sin catálogo o sin sobre de
    compuerta, nodo sin cámaras, configuración guardada no válida): el alta es transitoria y
    **no** consume el código."""


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeConfiguration:
    """``NodeConfiguration`` 🔒 de un nodo (o los valores por defecto de D-11)."""

    time_sources: tuple[str, ...] = DEFAULT_TIME_SOURCES
    sent_records_retention_days: int = 30
    token_max_age_seconds: int = 600
    heartbeat_interval_seconds: int = 60

    def __post_init__(self) -> None:
        sources = self.time_sources
        if (
            not isinstance(sources, tuple)
            or not 1 <= len(sources) <= 8
            or not all(isinstance(s, str) and _TIME_SOURCE.fullmatch(s) for s in sources)
        ):
            raise ValueError("time_sources son de 1 a 8 anfitriones o direcciones")
        _bounded(self.sent_records_retention_days, 1, 90, "sent_records_retention_days")
        # Nunca menos que la vigencia del token de vista de U-02 (600 s): el nodo rechazaría
        # tokens legítimos.
        _bounded(self.token_max_age_seconds, 600, 3_600, "token_max_age_seconds")
        _bounded(self.heartbeat_interval_seconds, 15, 600, "heartbeat_interval_seconds")

    @property
    def mute_after_seconds(self) -> int:
        """Siempre ``5 * heartbeat_interval_seconds`` (A-04): nunca un valor independiente."""
        return MUTE_FACTOR * self.heartbeat_interval_seconds


def _bounded(value: object, low: int, high: int, name: str) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} debe estar entre {low} y {high}")


@dataclass(frozen=True, slots=True)
class StoredCamera:
    """Una fila de ``catalog.zone_camera``: la cámara y su flujo en la configuración local."""

    camera_id: uuid.UUID
    stream_reference: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BootstrapZone:
    """Lo guardado de una zona asignada: sus dos sobres y sus cámaras (``None`` si falta)."""

    zone_id: uuid.UUID
    catalog_envelope: Mapping[str, Any] | None
    gate_envelope: Mapping[str, Any] | None
    cameras: tuple[StoredCamera, ...] = field(default=())


def _catalog_cameras(envelope: Mapping[str, Any]) -> list[tuple[uuid.UUID, str]]:
    payload = envelope.get("payload")
    cameras = payload.get("cameras") if isinstance(payload, Mapping) else None
    if not isinstance(cameras, list):
        raise ConfigurationUnavailable("el catálogo guardado no tiene cámaras")
    listed: list[tuple[uuid.UUID, str]] = []
    for camera in cameras:
        if not isinstance(camera, Mapping):
            raise ConfigurationUnavailable("una cámara del catálogo guardado no es un objeto")
        try:
            listed.append((uuid.UUID(str(camera["camera_id"])), str(camera["code"])))
        except (KeyError, ValueError):
            raise ConfigurationUnavailable("una cámara del catálogo no tiene id y código") from None
    return listed


def _cameras(zones: Sequence[BootstrapZone]) -> list[dict[str, str]]:
    seen: dict[uuid.UUID, dict[str, str]] = {}
    for zone in zones:
        if zone.catalog_envelope is None:
            raise ConfigurationUnavailable("una zona asignada no tiene catálogo")
        streams = {camera.camera_id: camera.stream_reference for camera in zone.cameras}
        for camera_id, code in _catalog_cameras(zone.catalog_envelope):
            if camera_id in seen:
                continue
            stream = streams.get(camera_id)
            if stream is None or not _STREAM_REFERENCE.fullmatch(stream):
                raise ConfigurationUnavailable("una cámara del catálogo no tiene su flujo")
            seen[camera_id] = {
                "camera_id": str(camera_id),
                "code": code,
                "stream_reference": stream,
            }
    return list(seen.values())


def initial_configuration(
    configuration: NodeConfiguration,
    zones: Sequence[BootstrapZone],
    *,
    ingest_base_url: str,
) -> dict[str, Any]:
    """``NodeInitialConfiguration`` del contrato como JSON (sin validar todavía con U-01).

    ``ConfigurationUnavailable`` si el nodo no tiene zonas, si a una zona le falta el catálogo o
    el sobre de compuerta, o si no hay cámaras: el alta responde transitorio sin consumir el
    código (decisión del redactor de TASK-219).
    """
    if not zones:
        raise ConfigurationUnavailable("el nodo no tiene zonas asignadas")
    missing = [
        zone.zone_id
        for zone in zones
        if zone.catalog_envelope is None or zone.gate_envelope is None
    ]
    if missing:
        raise ConfigurationUnavailable("una zona asignada no tiene catálogo o sobre de compuerta")
    cameras = _cameras(zones)
    if not cameras:
        raise ConfigurationUnavailable("el nodo no tiene cámaras en sus catálogos")
    return {
        "zones": [dict(zone.catalog_envelope or {}) for zone in zones],
        "gate_states": [dict(zone.gate_envelope or {}) for zone in zones],
        "cameras": cameras,
        "endpoints": {"ingest_base_url": ingest_base_url},
        "heartbeat": {
            "interval_seconds": configuration.heartbeat_interval_seconds,
            "mute_after_seconds": configuration.mute_after_seconds,
        },
        "gate_cache_ttl_seconds": GATE_CACHE_TTL_SECONDS,
        "sent_records_retention_days": configuration.sent_records_retention_days,
        "time_sources": list(configuration.time_sources),
        "live_view": {"token_max_age_seconds": configuration.token_max_age_seconds},
        "credential": {
            "validity_days": VALIDITY_DAYS,
            "rotate_before_days": ROTATE_BEFORE_DAYS,
            "alert_before_days": ALERT_BEFORE_DAYS,
        },
    }
