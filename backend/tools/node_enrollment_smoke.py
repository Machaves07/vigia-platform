"""Humo acotado del alta contra la API real (TASK-548, VIG-408).

Usa las credenciales y SyncClient del contrato, no el StubAdmin del kit. Todo el
estado del nodo es efímero; nunca conserva ni imprime respuestas o excepciones.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import secrets
import ssl
import tempfile
import uuid
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import httpx
from vigia_contracts.client import ClientConfig, SyncClient
from vigia_contracts.client.heartbeat import NodeReport, ZoneReport
from vigia_contracts.client.uploader import ClipUploadError
from vigia_contracts.clock import Clock
from vigia_contracts.credentials import (
    CredentialRejectedError,
    FileCredentialStore,
    HttpCredentialTransport,
    NodeCredentials,
    NodeIdentity,
)
from vigia_contracts.models.api import (
    parse_clip_upload_request,
    parse_observability_event_submission,
)
from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.outbox import Outbox
from vigia_contracts.transport import Client as TransportClient
from vigia_contracts.versioning import CONTRACT_VERSION

from vigia_platform.shared.clock import SystemClock

TIMEOUT = 60.0
CLIP_UNAVAILABLE = 3


class SmokeFailure(Exception):
    """Fallo local con código cerrado, nunca un mensaje remoto."""


class Parser(argparse.ArgumentParser):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs, add_help=False)
        self._optionals.title = "opciones"
        self.add_argument("-h", "--help", action="help", help="muestra esta ayuda y termina")

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "uso:", 1)

    def error(self, message: str) -> NoReturn:
        # argparse podría repetir el código o un argumento sensible desconocido.
        self.exit(2, "Argumentos inválidos; consulte --help\n")


def uuid7(clock: Clock) -> str:
    """UUID v7 sintético derivado del mismo reloj que los registros."""
    value = (int(clock.now().timestamp() * 1000) << 80) | secrets.randbits(80)
    value = (value & ~(15 << 76)) | (7 << 76)
    value = (value & ~(3 << 62)) | (2 << 62)
    return str(uuid.UUID(int=value))


def stamp(clock: Clock) -> str:
    return clock.now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def report_for(configuration: Any, live_view_url: str | None) -> NodeReport:
    cameras = {
        camera["camera_id"]: camera
        for envelope in configuration.zones
        for camera in envelope.payload.to_json_value()["cameras"]
    }
    return NodeReport(
        node_clock={"synchronized": True, "offset_ms": 0, "source": "ntp_local"},
        software_version="1.0.0",
        model_version="humo-sintetico-1",
        uptime_seconds=1,
        cameras=[
            {
                "camera_id": camera["camera_id"],
                "connected": True,
                "measured_fps": camera["declared_min_fps"],
                "declared_min_fps": camera["declared_min_fps"],
                "observability_state": "observable",
            }
            for camera in cameras.values()
        ],
        zones=[ZoneReport(e.payload.zone_id, "observable") for e in configuration.zones],
        # Nunca simular un adaptador en modo productivo: P9, simulated_adapter_in_productive.
        signal_reader={"available": False, "adapter": "modbus_rtu"},
        live_view_local_url=live_view_url,
    )


def exercise(
    args: argparse.Namespace, clock: Clock, directory: Path, result: dict[str, Any]
) -> None:
    origin = urlsplit(args.base_url)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        raise SmokeFailure("invalid_base_url")
    identity = NodeIdentity(args.node_id, args.organization_id, args.plant_id)
    host = urlsplit(args.live_view_url).hostname if args.live_view_url else "192.168.1.10"
    credentials = NodeCredentials(
        identity,
        FileCredentialStore(directory / "credentials.json"),
        clock,
        live_view_host=host or "192.168.1.10",
        software_version="1.0.0",
    )
    tls = ssl.create_default_context(cafile=args.ca_file)
    base = args.base_url.rstrip("/") + "/api/nodes"
    with httpx.Client(base_url=base, verify=tls, timeout=TIMEOUT, follow_redirects=False) as http:
        transport = TransportClient(base, base, timeout=httpx.Timeout(TIMEOUT))
        transport.set_enrollment_httpx_client(http)
        enrolled = credentials.enroll(args.code, "1" * 64, HttpCredentialTransport(transport))
    result["enrollment"] = "accepted"
    # La vista usa la CA del nodo, distinta de la raíz TLS de app./nodes.
    tls.load_verify_locations(cadata=enrolled.ca_chain)
    configuration = enrolled.initial_configuration
    config = ClientConfig.from_initial_configuration(
        configuration,
        cafile=args.ca_file,
        connect_timeout_seconds=TIMEOUT,
        read_timeout_seconds=TIMEOUT,
        upload_timeout_seconds=TIMEOUT,
    )
    report = report_for(configuration, args.live_view_url)
    with (
        Outbox(directory / "outbox.sqlite", clock, lock_timeout=TIMEOUT) as outbox,
        SyncClient(config, credentials, outbox, clock, status=lambda: report) as client,
    ):
        client.seed(configuration)
        for _ in range(3):
            before = len(client.events)
            client.heartbeat_now()
            beats = [e for e in client.events[before:] if e.operation == "heartbeat"]
            if len(beats) != 1 or beats[0].result != "success":
                raise SmokeFailure("heartbeat_rejected")
            result["heartbeats_accepted"] += 1
        zone = configuration.zones[0].payload.to_json_value()
        camera = zone["cameras"][0]["camera_id"]
        at = stamp(clock)
        event = parse_observability_event_submission(
            json.dumps(
                {
                    "event_id": uuid7(clock),
                    "contract_version": str(CONTRACT_VERSION),
                    "organization_id": identity.organization_id,
                    "plant_id": identity.plant_id,
                    "zone_id": zone["zone_id"],
                    "node_id": identity.node_id,
                    "subject": {"kind": "camera", "camera_id": camera},
                    "phase": "opened",
                    "state": "degraded",
                    "causes": ["frame_rate_insufficient"],
                    "started_at": at,
                    "node_time": {
                        "started_at": at,
                        "ended_at": at,
                        "clock": {"synchronized": True, "offset_ms": 0, "source": "ntp_local"},
                    },
                    "evidence": [],
                    "software_version": "1.0.0",
                }
            ).encode()
        )
        outbox.enqueue(event)
        if client.submit_record(event.event_id).result != "success":
            raise SmokeFailure("observability_event_rejected")
        result["observability_event"] = "accepted"
        # Contenido sintético: prueba transporte y confirmación, no reproducción ni anonimización.
        content = b"Clip sintetico de verificacion VIG-408" * 32
        clip = directory / "verification.mp4"
        clip.write_bytes(content)
        request = parse_clip_upload_request(
            json.dumps(
                {
                    "clip_id": uuid7(clock),
                    "camera_id": camera,
                    "zone_id": zone["zone_id"],
                    "media_kind": "video",
                    "content_type": "video/mp4",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "duration_ms": 10_000,
                    "purpose": "verification",
                }
            ).encode()
        )
        # Una sola operación acotada: NO run_forever ni reintento del 503 conocido.
        receipt = client.upload_verification_clip(clip, request)
        if receipt.clip_id != request.clip_id or receipt.sha256 != request.sha256:
            raise SmokeFailure("verification_clip_unconfirmed")
        result["verification_clip"] = "confirmed"
    if args.live_view_url:
        # GET a la misma URL anunciada; sin tokens, cookies ni credencial cliente de plataforma.
        with httpx.Client(verify=tls, timeout=TIMEOUT, follow_redirects=False) as live:
            response = live.get(args.live_view_url)
            if response.status_code != 200:
                raise SmokeFailure("live_view_unavailable")
        result["live_view"] = "reachable"


def main(argv: list[str] | None = None, *, clock: Clock | None = None) -> int:
    parser = Parser(description="Comprueba un nodo sintético contra la API real, sin conservarlo.")
    parser.add_argument("--base-url", required=True, help="Origen de app., sin /api/nodes")
    parser.add_argument("--code", required=True, help="Código de alta efímero; no se imprime")
    parser.add_argument("--node-id", required=True, help="UUID declarado en SCR-07")
    parser.add_argument("--organization-id", required=True, help="UUID de la organización cliente")
    parser.add_argument("--plant-id", required=True, help="UUID de la planta declarada")
    parser.add_argument("--ca-file", help="Raíces PEM de TLS; por omisión, las del sistema")
    parser.add_argument("--live-view-url", help="URL anunciada de un servidor local sintético")
    args = parser.parse_args(argv)
    result: dict[str, Any] = {
        "enrollment": "not_run",
        "heartbeats_accepted": 0,
        "observability_event": "not_run",
        "verification_clip": "not_run",
        "live_view": "not_requested",
        "code": "ok",
    }
    exit_code = 0
    # httpx registra las URL prefirmadas a nivel INFO. La herramienta es un proceso
    # independiente: se silencian los registros durante el intercambio y se restaura el nivel.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="vigia-smoke-") as temporary:
            exercise(args, clock or SystemClock(), Path(temporary), result)
    except CredentialRejectedError as error:
        result["code"] = error.code.value
        exit_code = 1
    except ClipUploadError as error:
        result["code"] = "verification_clip_failed"
        exit_code = 1
        if error.code == RejectionCode.TEMPORARILY_UNAVAILABLE:
            result["code"] = "clip_storage_unpublished"
            result["message_es"] = (
                "503 temporarily_unavailable al subir el clip: síntoma conocido de la composición; "
                "revise VIG-407 / TASK-562. No se reintenta."
            )
            exit_code = CLIP_UNAVAILABLE
    except SmokeFailure as error:
        result["code"] = str(error)
        exit_code = 1
    except Exception:
        # Incluye errores de transporte, esquema, certificado y disco; nunca su texto remoto.
        result["code"] = "smoke_failed"
        exit_code = 1
    finally:
        logging.disable(previous)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
