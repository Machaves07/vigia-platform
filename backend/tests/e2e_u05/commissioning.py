"""Precondiciones sintéticas del selector de clips, por las rutas reales del nodo."""

from __future__ import annotations

import hashlib
import ssl
import uuid

import httpx
from vigia_contracts.credentials import FileCredentialStore, NodeCredentials, NodeIdentity
from vigia_contracts.versioning import CONTRACT_VERSION

from tests.conformance.platform_target import WALL
from tests.factories import uuid7
from tests.load.conftest import LoadTarget
from tests.load.provision import LIVE_VIEW_HOST, SOFTWARE_VERSION, Fleet


def commissioning_clips(target: LoadTarget, fleet: Fleet) -> None:
    """Sube y confirma un clip por zona sin cambiar las rutas ni el almacén de producción.

    Como ``GobScenario.verification_clip``, el contenido es sintético y no se reproduce:
    el recorrido comprueba su selección como evidencia, no la guarda de cierre del acta.
    La credencial efímera del nodo la gestiona el mismo cliente que utiliza la carga.
    """
    for node in fleet.nodes:
        credentials = NodeCredentials(
            NodeIdentity(str(node.node_id), str(fleet.organization_id), str(node.plant_id)),
            FileCredentialStore(node.credential_file),
            WALL,
            live_view_host=LIVE_VIEW_HOST,
            software_version=SOFTWARE_VERSION,
        )
        tls = credentials.client_ssl_context(cafile=str(target.tls.ca_file))
        with httpx.Client(
            base_url=target.nodes.url,
            verify=tls,
            timeout=60,
            headers={"X-Vigia-Contract-Version": str(CONTRACT_VERSION)},
        ) as client:
            for zone in node.zone_ids:
                content = b"Clip sintetico del selector de comisionamiento" * 16
                clip_id = uuid7()
                response = client.post(
                    "/api/nodes/clip-uploads",
                    json={
                        "clip_id": str(clip_id),
                        "camera_id": str(uuid.uuid5(zone, "camara-0")),
                        "zone_id": str(zone),
                        "media_kind": "video",
                        "content_type": "video/mp4",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                        "duration_ms": 10_000,
                        "purpose": "verification",
                    },
                )
                assert response.status_code == 200, "No se concedió el clip sintético"
                grant = response.json()
                upload = grant.get("upload", grant)
                with httpx.Client(
                    verify=ssl.create_default_context(cafile=str(target.tls.ca_file)), timeout=60
                ) as store:
                    uploaded = store.put(
                        upload.get("upload_url", upload.get("url")),
                        content=content,
                        headers=upload["required_headers"],
                    )
                    assert uploaded.status_code == 200, "No se subió el clip sintético"
                confirmed = client.post(f"/api/nodes/clip-uploads/{clip_id}/confirmation")
                assert confirmed.status_code == 200, "No se confirmó el clip sintético"
