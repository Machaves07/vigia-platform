"""Puerta de ``vigia-mtls-edge`` en la composición de integración (TASK-545, VIG-393).

Docker publica los puertos del balanceador en ``VIGIA_EDGE_PUBLISH_IP``, que
``compose.integration.yml`` toma de ``VIGIA_EDGE_BIND``. Si esa IP es ``0.0.0.0``, ``::`` o no es
una dirección, el balanceador quedaría escuchando en todas las interfaces de la máquina (la LAN,
la tailnet y lo que haya), y el contenedor se niega a arrancar. Una IP de bucle local (la de la
prueba) o la de la tailnet valen.

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

ENVIRONMENT_VARIABLE = "VIGIA_EDGE_PUBLISH_IP"


def check_publish_ip(environ: Mapping[str, str]) -> str | None:
    """El motivo de rechazo en español, o ``None`` si la IP de publicación es aceptable."""
    raw = environ.get(ENVIRONMENT_VARIABLE, "").strip()
    if not raw:
        return f"falta {ENVIRONMENT_VARIABLE}: el balanceador solo se publica en una IP concreta"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return f"{ENVIRONMENT_VARIABLE} no es una dirección IP"
    if address.is_unspecified:
        return f"{ENVIRONMENT_VARIABLE} es {address}: publicaría en todas las interfaces"
    if address.is_multicast:
        return f"{ENVIRONMENT_VARIABLE} es una dirección multicast"
    return None


def main() -> int:
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
