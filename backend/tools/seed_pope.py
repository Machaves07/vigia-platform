"""Semilla HTTP de Pope para integración (TASK-547); nunca accede a la base.

Las sesiones verificadas se reciben por entorno, no por argumentos ni archivos. El código
de alta solo se escribe una vez en stdout. Ejecutar de forma secuencial, sin redirigir stdout
a un archivo. Véanse las decisiones de reanudación y los límites del fixture en pope/SOURCE.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import struct
import sys
import zlib
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TextIO

import httpx
import rfc8785

from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME

CATALOG_DIRECTORY = Path(__file__).parent / "pope"
CATALOG_NAMES = ("zone_catalog.json", "zone_catalog.pope_foso.json")
ADMIN_SESSION = "VIGIA_POPE_ADMIN_SESSION"
INSTALLER_SESSION = "VIGIA_POPE_INSTALLER_SESSION"
REASON = "Siembra sintética del catálogo Pope para integración en sombra"
NODE_CODE = "POPE-NODE"
TIMEOUT_SECONDS = 60.0
PARAMETERS = ("cameras", "minimum_coverage", "signals", "thresholds", "clip_window", "episode")


class SeedError(Exception):
    """Error seguro para el operador: no contiene cuerpos HTTP, sesiones ni códigos de alta."""


def comparable_catalog(catalog: Mapping[str, Any]) -> bytes:
    """RFC 8785, excluyendo solo metadatos que las rutas asignan (no cualquier *_id).

    Cámara y señal conservan sus ids: la API los recibe, no los asigna. En los estándares
    también son asignados la declaración (autor, nombre y rol) y su vigencia efectiva.
    Se conserva el orden de todas las listas del contrato.
    """
    assigned = {"zone_id", "plant_id", "organization_id", "version", "issued_at"}
    standard_assigned = {"standard_id", "version", "declared_by", "effective_from"}
    document = {key: value for key, value in catalog.items() if key not in assigned}
    document["standards"] = [
        {key: value for key, value in standard.items() if key not in standard_assigned}
        for standard in catalog["standards"]
    ]
    return rfc8785.dumps(document)


def load_catalogs(path: Path) -> tuple[dict[str, Any], ...]:
    """Lee los dos fixtures contiguos, sin escribir ni corregir su semántica (A-03)."""
    paths = (path, path.with_name(CATALOG_NAMES[1]))
    catalogs = tuple(json.loads(item.read_bytes()) for item in paths)
    # No usar el lector semántico del contrato aquí: A-03 debe llegar a la API y producir
    # catalog_unsatisfiable_coverage, no quedar oculto como una validación local.
    if tuple(catalog["zone_code"] for catalog in catalogs) != ("POPE", "POPE-FOSO"):
        raise SeedError("Los catálogos deben corresponder a POPE y POPE-FOSO")
    return catalogs


def synthetic_capture() -> bytes:
    """PNG opaco de 8x8 sin personas: soporte explícitamente sintético, nunca una foto real."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * 24) * 8))
        + chunk(b"IEND", b"")
    )


class PopeSeeder:
    """Orquesta exclusivamente rutas HTTP con los permisos de cada sesión.

    No promete exclusión entre operadores: una sola ejecución a la vez. Las restricciones
    y transacciones de las rutas siguen siendo la autoridad frente a conflictos.
    """

    def __init__(
        self,
        administrator: httpx.Client,
        installer: httpx.Client,
        uploads: httpx.Client,
        output: TextIO,
    ) -> None:
        self.administrator = administrator
        self.installer = installer
        self.uploads = uploads
        self.output = output
        self.writes = 0

    def request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        optional: bool = False,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        response = client.request(method, path, json=body, params=params)
        if optional and response.status_code == 404:
            return None
        if response.is_error:
            # Nunca interpolar response.text, request, cabeceras ni la excepción de httpx.
            detail = response.json().get("detail_code")
            if detail == "catalog_unsatisfiable_coverage":
                raise SeedError("unsatisfiable_coverage: la plataforma rechazó la cobertura")
            raise SeedError(f"La plataforma rechazó una operación HTTP ({response.status_code})")
        if method != "GET":
            self.writes += 1
        result: dict[str, Any] = response.json()
        return result

    def required(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        result = self.request(client, method, path, body, params=params)
        if result is None:
            raise SeedError("Falta la respuesta de la plataforma")
        return result

    def admissions(self, plant: str) -> None:
        path = f"/plants/{plant}/admissions"
        admitted: set[str] = set()
        params: dict[str, str] = {}
        while True:
            page = self.required(self.administrator, "GET", path, params=params)
            admitted.update(a["family"] for a in page["admissions"] if a["result"] == "admitted")
            if page["next_after"] is None:
                break
            params = {"after": page["next_after"]}
        for family in ("coexistence", "dwell", "guard_bypass", "startup_transition"):
            if family not in admitted:
                self.required(
                    self.administrator,
                    "POST",
                    path,
                    {
                        "family": family,
                        "answers": dict.fromkeys(("standard", "remedy", "subject"), True),
                    },
                )

    def publish(self, zone: str, source: dict[str, Any]) -> None:
        path = f"/zones/{zone}"
        current = self.request(self.administrator, "GET", path + "/catalog", optional=True)
        cameras = [
            {**camera, "stream_reference": camera["code"].lower()} for camera in source["cameras"]
        ]
        parameters = {key: source[key] for key in PARAMETERS}
        parameters["cameras"] = cameras
        for standard in source["standards"]:
            # El contrato no tiene code para estándares: título declarado estable, sin inventarlo.
            existing = [] if current is None else current["catalog"]["standards"]
            found = next((s for s in existing if s["title_es"] == standard["title_es"]), None)
            fields = ("family", "title_es", "declared_text", "predicate")
            draft = {key: standard[key] for key in fields}
            if found is not None:
                if rfc8785.dumps(draft) != rfc8785.dumps({key: found[key] for key in fields}):
                    raise SeedError("Un estándar existente difiere del origen; no se sobrescribe")
                continue
            body = {**draft, "reason_es": REASON}
            if current is None:
                body["zone_parameters"] = parameters
            current = self.required(self.administrator, "POST", path + "/standards", body)
        for route, values, keys in (
            ("cameras", {"cameras": cameras}, ("cameras",)),
            ("minimum-coverage", source["minimum_coverage"], ("minimum_coverage",)),
            ("signals", {"signals": source["signals"]}, ("signals",)),
            ("thresholds", source["thresholds"], ("thresholds",)),
            (
                "windows",
                {key: source[key] for key in ("clip_window", "episode")},
                ("clip_window", "episode"),
            ),
        ):
            if current is not None and any(
                rfc8785.dumps(current["catalog"][key]) != rfc8785.dumps(source[key]) for key in keys
            ):
                current = self.required(
                    self.administrator, "PUT", path + "/" + route, {**values, "reason_es": REASON}
                )
        if current is None or comparable_catalog(current["catalog"]) != comparable_catalog(source):
            raise SeedError("El catálogo publicado no coincide canónicamente con el origen")

    def mounting(self, zone: str, plant: str, source: dict[str, Any]) -> None:
        path = f"/zones/{zone}/gates"
        state = self.required(self.installer, "GET", path)
        if state["usage"]["status"] != "pending":
            raise SeedError("La compuerta de uso no está pendiente; se requiere revisión humana")
        if state["mounting"]["status"] == "approved":
            return
        if state["mounting"]["status"] != "pending":
            raise SeedError("No se reaprueba automáticamente un montaje revocado")
        capture = synthetic_capture()
        grant = self.required(
            self.installer,
            "POST",
            "/documents",
            {
                "plant_id": plant,
                "kind": "blur_check_capture",
                "content_type": "image/png",
                "size_bytes": len(capture),
                "sha256": hashlib.sha256(capture).hexdigest(),
            },
        )
        upload = grant["upload"]
        response = self.uploads.put(
            upload["url"], headers=upload["required_headers"], content=capture
        )
        if response.is_error:
            raise SeedError("No se pudo subir la captura sintética de integración")
        self.required(
            self.installer,
            "POST",
            path + "/mounting/scope-record",
            {
                "scope_text_es": (
                    "Acta sintética de integración Pope; no acredita el montaje de planta."
                ),
                "cameras": [
                    {
                        "camera_id": camera["camera_id"],
                        "framing_description_es": (
                            "Encuadre sintético de integración, sin imágenes de planta."
                        ),
                        "reference_marker": True,
                    }
                    for camera in source["cameras"]
                ],
                "blur_verification": {
                    "declared": True,
                    "capture_document_ref": grant["document_ref"],
                },
            },
        )

    def seed(self, plant: str, catalogs: Sequence[dict[str, Any]]) -> None:
        hierarchy = self.required(self.administrator, "GET", "/hierarchy")
        target = next((p for p in hierarchy["plants"] if p["plant_id"] == plant), None)
        if target is None:
            raise SeedError("La planta no está al alcance de la sesión administradora")
        self.admissions(plant)
        zones: list[str] = []
        for source in catalogs:
            zone = next((z for z in target["zones"] if z["code"] == source["zone_code"]), None)
            if zone is None:
                zone = self.required(
                    self.administrator,
                    "POST",
                    f"/plants/{plant}/zones",
                    {"code": source["zone_code"], "name": source["zone_code"]},
                )
            zone_id = str(zone["zone_id"])
            zones.append(zone_id)
            # Un rechazo detiene toda la semilla antes del nodo, actas y código de alta.
            self.publish(zone_id, source)
        node = next(
            (n for n in hierarchy["nodes"] if n["plant_id"] == plant and n["code"] == NODE_CODE),
            None,
        )
        new_node = node is None
        if node is None:
            node = self.required(
                self.installer,
                "POST",
                f"/plants/{plant}/nodes",
                {"code": NODE_CODE, "zone_ids": zones},
            )
        else:
            inventory = self.required(self.installer, "GET", f"/fleet/nodes/{node['node_id']}")[
                "node"
            ]
            if set(inventory["zones"]) != set(zones):
                raise SeedError("El nodo existente no tiene exactamente las zonas Pope")
        for zone_id, source in zip(zones, catalogs, strict=True):
            self.mounting(zone_id, plant, source)
        if new_node:
            issued = self.required(
                self.installer, "POST", f"/nodes/{node['node_id']}/enrollment-codes"
            )
            print(
                json.dumps({"event": "enrollment_code", **issued}, ensure_ascii=False),
                file=self.output,
            )
        print(
            json.dumps(
                {
                    "event": "seeded" if self.writes else "unchanged",
                    "message_es": "Semilla completada" if self.writes else "sin cambios",
                    "writes": self.writes,
                    "node_id": node["node_id"],
                    "zone_ids": zones,
                },
                ensure_ascii=False,
            ),
            file=self.output,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Siembra Pope por la API con dos sesiones verificadas."
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG_DIRECTORY / CATALOG_NAMES[0],
        help="Catálogo Pope; el de pope_foso se lee de la misma carpeta",
    )
    parser.add_argument("--base-url", help="Origen de la API HTTP (sin ruta final)")
    parser.add_argument("--plant", help="Identificador de la planta sintética de integración")
    parser.add_argument("--concession", help="Identificador de la concesión vigente del instalador")
    parser.add_argument("--ca-bundle", type=Path, help="CA de confianza para la API y las subidas")
    parser.add_argument(
        "--dry-run", action="store_true", help="Imprime el plan sin red ni escrituras"
    )
    args = parser.parse_args(argv)
    try:
        catalogs = load_catalogs(args.catalog)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "event": "plan",
                        "families": 4,
                        "zones": [
                            {
                                "code": c["zone_code"],
                                "cameras": [camera["code"] for camera in c["cameras"]],
                                "standards": len(c["standards"]),
                            }
                            for c in catalogs
                        ],
                        "node_code": NODE_CODE,
                        "mounting": "approved",
                        "usage": "pending",
                        "enrollment": "solo al declarar el nodo",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if not all((args.base_url, args.plant, args.concession)):
            raise SeedError("Se requieren --base-url, --plant y --concession")
        if not os.environ.get(ADMIN_SESSION) or not os.environ.get(INSTALLER_SESSION):
            raise SeedError(
                f"Se requieren las sesiones verificadas {ADMIN_SESSION} e {INSTALLER_SESSION}"
            )
        verify = ssl.create_default_context(cafile=args.ca_bundle)
        with ExitStack() as stack:
            clients = [
                stack.enter_context(
                    httpx.Client(
                        base_url=args.base_url,
                        verify=verify,
                        timeout=TIMEOUT_SECONDS,
                        headers={
                            "Sec-Fetch-Site": "same-origin",
                            "Cookie": f"{SESSION_COOKIE_NAME}={os.environ[variable]}",
                            **extra,
                        },
                    )
                )
                for variable, extra in (
                    (ADMIN_SESSION, {}),
                    (INSTALLER_SESSION, {"X-Vigia-Concession": args.concession}),
                )
            ]
            # La subida no hereda las sesiones de consola ni la cabecera de concesión.
            uploads = stack.enter_context(httpx.Client(verify=verify, timeout=TIMEOUT_SECONDS))
            PopeSeeder(clients[0], clients[1], uploads, sys.stdout).seed(args.plant, catalogs)
        return 0
    except SeedError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        print("No se pudo completar la semilla; revise los insumos y la conexión", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
