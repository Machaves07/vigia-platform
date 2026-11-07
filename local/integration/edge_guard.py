"""Puerta de ``vigia-mtls-edge`` en la composición de integración (TASK-545, VIG-393).

Docker publica los puertos del balanceador en ``VIGIA_EDGE_PUBLISH_IP``, que
``compose.integration.yml`` toma de ``VIGIA_EDGE_BIND``. Solo se acepta la IP de la tailnet o el
bucle local (``allowed``: lista de aceptación, no de rechazo): ``0.0.0.0``, ``::``, la LAN, una IP
pública, ``::ffff:0.0.0.0`` y cualquier cosa que no sea una dirección dejarían al balanceador
escuchando donde no debe, y el contenedor se niega a arrancar. Una dirección IPv4 mapeada en IPv6
(``::ffff:a.b.c.d``) se compara como la IPv4 que es, porque Docker la normaliza así.

Con ``--check <ip>`` solo valida (``lib.sh`` lo usa antes de tocar nada): sale con 0 o con 2 y el
motivo en la salida de errores.

Después crea el certificado de servidor si no existe (``vigia-mtls-edge init-cert``: idempotente,
con los nombres ``app.`` y ``nodes.`` de ``VIGIA_DOMAIN`` y la IP de publicación; si ya existe no lo
toca) y sustituye el proceso por ``vigia-mtls-edge serve`` con el mismo entorno.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Final

ENVIRONMENT_VARIABLE: Final = "VIGIA_EDGE_PUBLISH_IP"
TAILNET_V4: Final = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6: Final = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def allowed(value: str) -> str | None:
    """El motivo de rechazo de ``value`` como IP de publicación, o ``None`` si se acepta."""
    text = value.strip()
    if not text:
        return "vacía: el balanceador solo se publica en una IP concreta"
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return "no es una dirección IP"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return None
    if isinstance(address, ipaddress.IPv4Address) and address in TAILNET_V4:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address in TAILNET_V6:
        return None
    return f"{address} no es el bucle local ni la IP de la tailnet (100.64.0.0/10)"


def check_publish_ip(environ: Mapping[str, str]) -> str | None:
    """El motivo de rechazo en español, o ``None`` si la IP de publicación es aceptable."""
    reason = allowed(environ.get(ENVIRONMENT_VARIABLE, ""))
    if reason is None:
        return None
    return f"{ENVIRONMENT_VARIABLE}: {reason}"


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        reason = allowed(sys.argv[2])
        if reason is not None:
            print(f"edge_guard: {reason}", file=sys.stderr)
            return 2
        return 0
    problem = check_publish_ip(os.environ)
    if problem is not None:
        print(f"edge_guard: {problem}", file=sys.stderr)
        return 2
    domain = os.environ.get("VIGIA_DOMAIN", "").strip()
    if not domain:
        print("edge_guard: falta VIGIA_DOMAIN", file=sys.stderr)
        return 2
    created = subprocess.run(  # noqa: S603 - orden fija de la imagen
        [  # noqa: S607
            "vigia-mtls-edge",
            "init-cert",
            "--san",
            f"app.{domain}",
            "--san",
            f"nodes.{domain}",
            "--ip",
            os.environ[ENVIRONMENT_VARIABLE].strip(),
        ],
        check=False,
    )
    if created.returncode != 0:
        return created.returncode
    os.execvp("vigia-mtls-edge", ["vigia-mtls-edge", "serve"])  # noqa: S606, S607
    return 0  # pragma: no cover - execvp no vuelve


if __name__ == "__main__":
    raise SystemExit(main())
