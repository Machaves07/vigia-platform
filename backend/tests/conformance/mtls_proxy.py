"""Balanceador local de la suite de conformidad: termina TLS como los dos balanceadores de U-03.

``infrastructure-design.md`` §5 de U-03: el de **nodos** (``nodes.``) termina mTLS en modo
*verify* contra el almacén de confianza de ``vigia-node-ca`` y pasa a la aplicación la hoja y sus
datos en las cabeceras ``X-Amzn-Mtls-Clientcert-{Leaf,Serial-Number,Subject,Issuer,Validity}``;
el de **personas** (``app.``) no pide certificado y quita cualquier ``X-Amzn-Mtls-*`` entrante
(U-02 §4.2). ``MtlsProxy`` hace lo mismo en local, en un hilo con su propio bucle:

- ``client_ca`` dado (``nodes.``): pide certificado de cliente (opcional, para que la petición sin
  certificado llegue a la aplicación y responda ``node_not_enrolled``) y lo verifica contra esa
  raíz en el saludo TLS; un certificado de otra autoridad no completa el saludo, como en el
  balanceador real. Con certificado, añade las cinco cabeceras (``alb_headers``).
- **Siempre** descarta las ``X-Amzn-Mtls-*`` que traiga el cliente, en los dos: la aplicación solo
  ve las que pone el balanceador.
- Reparte **por petición** en turno rotatorio entre ``backends`` (dos procesos de ``vigia-api`` en
  NFR-GOB-15); una conexión persistente del cliente no queda atada a un proceso.

Cada intercambio queda en ``exchanges`` (ruta, proceso, estado y las cabeceras ``X-Amzn-Mtls-*``
enviadas a la aplicación), nunca el cuerpo. ``server_tls`` crea la autoridad efímera del
certificado de servidor (``127.0.0.1``), nunca versionada (NFR-CTR-14).

El balanceador vive en ``tools/mtls_edge.py`` (TASK-542), que el entorno de integración ejecuta con
un certificado persistente; aquí queda lo que solo usan las pruebas: la autoridad efímera.

Solo datos generados.
"""

from __future__ import annotations

import datetime as dt
from ipaddress import IPv4Address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from tools.mtls_edge import (
    MTLS_HEADER_PREFIX,
    UPSTREAM_ERROR_HEADER,
    Exchange,
    MtlsProxy,
    ServerTls,
    mtls_proxy,
)

__all__ = [
    "MTLS_HEADER_PREFIX",
    "UPSTREAM_ERROR_HEADER",
    "Exchange",
    "MtlsProxy",
    "ServerTls",
    "mtls_proxy",
    "server_tls",
]


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Vigia"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def server_tls(directory: Path, now: dt.datetime) -> ServerTls:
    """Raíz efímera ``test-only`` y certificado de servidor para ``127.0.0.1`` y ``localhost``."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("Vigia balanceador local test-only")
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("127.0.0.1"))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(IPv4Address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    tls = ServerTls(
        directory / "balanceador-ca.crt",
        directory / "balanceador.crt",
        directory / "balanceador.key",
    )
    tls.ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    tls.certificate_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    tls.key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls.key_file.chmod(0o600)
    return tls
