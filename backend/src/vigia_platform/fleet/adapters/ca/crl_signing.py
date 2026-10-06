"""Firma de la lista de revocación global con ``vigia-node-ca`` (TASK-220; LC-GOB-11, NFR-GOB-48).

``NodeCaRevocationListSigner`` implementa ``RevocationListSignerPort``:

1. lee el paquete publicado ``ca/root.pem`` (1 o 2 raíces, D-6) y toma de él la raíz cuya clave
   pública es la de la clave KMS configurada, como la emisión de certificados
   (``certificate_profiles.NodeCaIssuer``): la lista la emite **la misma** raíz que firma los
   certificados de los nodos;
2. construye la lista (``build_revocation_list``): emisor = sujeto de esa raíz, ``last_update``,
   ``next_update`` (7 días), ``AuthorityKeyIdentifier`` de la clave de la raíz, ``CRLNumber`` y una
   entrada por credencial con su fecha y su motivo (``superseded`` para las sustituidas);
3. la firma con ``shared.node_ca.sign_revocation_list`` (``kms:Sign``; la clave privada nunca
   existe en el proceso) y comprueba la firma con la clave pública antes de devolverla.

Todo el tramo tiene un tope de 5 s (NFR-GOB-43). Cualquier fallo (KMS caído o que niega, paquete
ilegible, raíz que no casa con la clave, firma que no verifica) termina en
``RevocationListPublishFailed(SIGN)`` con un mensaje constante: nunca el PEM, el ARN ni un número
de serie. Durante una sustitución de raíz (D-6), la lista cubre los certificados de la raíz de la
clave configurada; la anterior deja de emitir al sustituirse.
"""

from __future__ import annotations

import asyncio
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from vigia_platform.fleet.adapters.ca.certificate_profiles import RootObjects
from vigia_platform.fleet.domain.revocation_list import (
    PUBLISH_STEP_TIMEOUT_SECONDS,
    PublishStep,
    RevocationListPlan,
    RevocationListPublishFailed,
    RevocationReason,
    SignedRevocationList,
)
from vigia_platform.shared.node_ca import (
    ROOT_CERTIFICATE_KEY,
    NodeCaError,
    NodeCaSigner,
    kms_public_key,
    read_bundle,
    sign_revocation_list,
)
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["NodeCaRevocationListSigner", "build_revocation_list", "issuing_root"]

_log = get_logger("fleet.revocation_list")

_REASONS: Final = {
    RevocationReason.REVOKED: x509.ReasonFlags.unspecified,
    RevocationReason.SUPERSEDED: x509.ReasonFlags.superseded,
}


def issuing_root(
    bundle: tuple[x509.Certificate, ...], public: ec.EllipticCurvePublicKey
) -> x509.Certificate:
    """La única raíz del paquete con la clave ``public``; ``NodeCaError`` si no hay una."""
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    expected = public.public_bytes(der, spki)
    matching = [
        root
        for root in bundle
        if isinstance(root.public_key(), ec.EllipticCurvePublicKey)
        and root.public_key().public_bytes(der, spki) == expected
    ]
    if len(matching) != 1:
        raise NodeCaError("ninguna raíz publicada es de la clave de la autoridad")
    return matching[0]


def build_revocation_list(
    plan: RevocationListPlan, issuer: x509.Certificate
) -> x509.CertificateRevocationListBuilder:
    """La lista sin firmar de ``plan`` emitida por la raíz ``issuer``."""
    issuer_key = issuer.public_key()
    if not isinstance(issuer_key, ec.EllipticCurvePublicKey):
        raise NodeCaError("la raíz de la autoridad no tiene clave EC")
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer.subject)
        .last_update(plan.last_update)
        .next_update(plan.next_update)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key), critical=False
        )
        .add_extension(x509.CRLNumber(plan.crl_number), critical=False)
    )
    for entry in plan.entries:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(entry.serial_number)
            .revocation_date(entry.revocation_date)
            .add_extension(x509.CRLReason(_REASONS[entry.reason]), critical=False)
            .build()
        )
    return builder


class NodeCaRevocationListSigner:
    """``RevocationListSignerPort`` sobre ``kms:Sign`` y el paquete publicado de raíces."""

    def __init__(
        self,
        *,
        kms: NodeCaSigner,
        key_id: str,
        roots: RootObjects,
        deadline_seconds: float = PUBLISH_STEP_TIMEOUT_SECONDS,
        root_key: str = ROOT_CERTIFICATE_KEY,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds debe ser positivo")
        self._kms = kms
        self._key_id = key_id
        self._roots = roots
        self._deadline = deadline_seconds
        self._root_key = root_key

    def __repr__(self) -> str:
        return "NodeCaRevocationListSigner()"

    async def sign(self, plan: RevocationListPlan) -> SignedRevocationList:
        try:
            async with asyncio.timeout(self._deadline):
                return await self._sign(plan)
        except TimeoutError:
            _log.warning("la autoridad de nodos no firmó la lista a tiempo")
        except Exception:
            # Ni el mensaje ni el PEM llegan al registro.
            _log.error("la autoridad de nodos no pudo firmar la lista de revocación")
        raise RevocationListPublishFailed(PublishStep.SIGN)

    async def _sign(self, plan: RevocationListPlan) -> SignedRevocationList:
        bundle = read_bundle(await self._roots.get_object(self._root_key))
        public = await kms_public_key(self._kms, self._key_id)
        root = issuing_root(bundle, public)
        signed = await sign_revocation_list(
            build_revocation_list(plan, root), self._kms, self._key_id, issuer_public_key=public
        )
        return SignedRevocationList(
            pem=signed.public_bytes(serialization.Encoding.PEM),
            crl_number=plan.crl_number,
            last_update=plan.last_update,
            next_update=plan.next_update,
            entries=len(plan.entries),
        )
