"""Fábrica única de clientes de salida de U-04 con lista cerrada de destinos (LC-LAZ-39).

Es la **segunda barrera** de NFR-LAZ-21 (PAT-LAZ-SEG-01): la regla 443 de ``sg-worker`` hacia
Internet no distingue destinos (nota U04-H-10), así que la barrera efectiva es esta. El proceso de
trabajo solo alcanza **dos** destinos de Internet, leídos de la configuración
(``RuntimeConfig``):

- la interfaz del proveedor del asistente (``VIGIA_ASSISTANT_API_HOST``, por defecto
  ``api.anthropic.com``), solo para el componente ``assistant_provider``;
- el servicio de correo (``VIGIA_EMAIL_API_HOST``, por defecto
  ``email.<AWS_REGION>.amazonaws.com``), solo para ``email_sender``.

Todo cliente de U-04 se construye aquí y **cada petición** se comprueba antes de salir: esquema
``https``, puerto 443 y el host del componente, exactos. Si no coincide, **falla cerrado**:
``EgressBlocked`` (no sale nada), ``loop_egress_blocked_total`` con el componente
(``EGRESS_BLOCKED_ALARM``) y un registro crítico que nombra el **componente y la ruta de código**
que intentó salir, lo que la barrera de red no puede decir. El destino intentado no se registra.

- ``httpx_client``: ``httpx.AsyncClient`` con la comprobación en el gancho ``request`` (también en
  cada redirección, que además se desactiva) y sin proxies del entorno.
- ``email_client``: cliente ``sesv2`` de boto3 contra el host del correo, con la comprobación en
  ``before-send`` de botocore (cada intento, también los reintentos).
- ``assistant_client``: el cliente del paquete del proveedor, construido con **sus valores por
  omisión de transporte**: la fábrica rechaza ``http_client`` y cualquier objeto de ``httpx``
  (tech-stack §2.1: el paquete usa su propia biblioteca HTTP y un objeto de ``httpx`` falla en
  tiempo de petición). La lista se aplica **al adaptador** (``GuardedClient.call``), no al
  transporte interno del paquete. El constructor lo pasa quien adapta al proveedor (TASK-334):
  esta tarea no añade la dependencia.

``tools/check_outbound_clients.py`` comprueba de forma estática que ningún módulo de U-04 construye
``httpx.AsyncClient``, un cliente de SES de boto3 o ``AsyncAnthropic`` fuera de este módulo
(PR-LAZ-54).
"""

from __future__ import annotations

import enum
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import boto3  # type: ignore[import-untyped]
import httpx

from vigia_platform.loop.observability.metrics import LoopMetrics, get_loop_metrics
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.runtime.config import EGRESS_HOST, RuntimeConfig, RuntimeConfigInvalid

__all__ = [
    "DEFAULT_ASSISTANT_HOST",
    "DEFAULT_REGION",
    "EgressAllowlist",
    "EgressBlocked",
    "EgressComponent",
    "GuardedClient",
    "HttpClientFactory",
    "default_email_host",
]

_log = get_logger("loop.http")

DEFAULT_ASSISTANT_HOST: Final = "api.anthropic.com"
DEFAULT_REGION: Final = "us-east-1"
_CODE_PATH: Final = re.compile(r"vigia_platform(?:\.[a-z][a-z0-9_]{0,40}){1,6}")
"""Ruta de código que construye un cliente: el módulo de U-04 que lo pide (una constante)."""
_HTTPX_OPTIONS_OWNED: Final = frozenset(
    {"base_url", "event_hooks", "follow_redirects", "mounts", "proxy", "proxies", "trust_env"}
)
"""Opciones de ``httpx.AsyncClient`` que fija la fábrica y nadie puede cambiar."""
_PROVIDER_OPTIONS_OWNED: Final = frozenset({"base_url", "http_client"})
_HTTPX_OBJECTS: Final = (
    httpx.Client,
    httpx.AsyncClient,
    httpx.BaseTransport,
    httpx.AsyncBaseTransport,
)


def default_email_host(region: str) -> str:
    """El punto de SES v2 de ``region`` (``email.us-east-1.amazonaws.com``)."""
    return f"email.{region}.amazonaws.com"


class EgressComponent(enum.StrEnum):
    """Los componentes de U-04 que salen a Internet (lista cerrada, dimensión de la métrica)."""

    ASSISTANT_PROVIDER = "assistant_provider"
    EMAIL_SENDER = "email_sender"


class EgressBlocked(Exception):
    """Una petición saliente con destino fuera de la lista cerrada: no salió (fallo cerrado).

    Nombra el componente y la ruta de código, nunca el destino. Dentro de un consumidor es un
    defecto del manejador (reintento y, al octavo, cola muerta con su alarma), no una caída de
    la dependencia.
    """

    code: Final = "egress_blocked"

    def __init__(self, component: EgressComponent, code_path: str) -> None:
        super().__init__(
            f"salida bloqueada: {component.value} ({code_path}) apuntó fuera de la lista cerrada "
            "de destinos (LC-LAZ-39)"
        )
        self.component = component
        self.code_path = code_path


@dataclass(frozen=True, slots=True)
class EgressAllowlist:
    """Los dos destinos de Internet del proceso de trabajo, cada uno de su componente."""

    assistant_host: str
    email_host: str

    def __post_init__(self) -> None:
        for host in (self.assistant_host, self.email_host):
            if not isinstance(host, str) or EGRESS_HOST.fullmatch(host) is None:
                raise ValueError("destino de salida mal formado")
        if self.assistant_host == self.email_host:
            raise ValueError("los dos destinos de salida deben ser distintos")

    @property
    def hosts(self) -> frozenset[str]:
        """La lista cerrada: exactamente dos destinos."""
        return frozenset({self.assistant_host, self.email_host})

    def host_for(self, component: EgressComponent) -> str:
        if component is EgressComponent.ASSISTANT_PROVIDER:
            return self.assistant_host
        return self.email_host

    def allows(self, component: EgressComponent, url: str) -> bool:
        """``url`` va por ``https`` al puerto 443 del host de ``component``, sin credenciales.

        El host se compara exacto (sin punto final ni subdominios), sin distinguir mayúsculas,
        como el DNS.
        """
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        return (
            parts.scheme == "https"
            and parts.username is None
            and parts.password is None
            and parts.hostname == self.host_for(component)
            and port in (None, 443)
        )

    @classmethod
    def from_config(cls, config: RuntimeConfig | None) -> EgressAllowlist:
        """La lista de ``config``; sin configuración (pruebas), los destinos del diseño.

        Raises:
            RuntimeConfigInvalid: los dos destinos coinciden.
        """
        region = config.aws_region if config is not None else DEFAULT_REGION
        assistant = (
            config.assistant_api_host
            if config is not None and config.assistant_api_host is not None
            else DEFAULT_ASSISTANT_HOST
        )
        email = (
            config.email_api_host
            if config is not None and config.email_api_host is not None
            else default_email_host(region)
        )
        try:
            return cls(assistant_host=assistant, email_host=email)
        except ValueError:
            raise RuntimeConfigInvalid(
                "VIGIA_EMAIL_API_HOST", "coincide con el destino del asistente"
            ) from None


class GuardedClient[T]:
    """Un cliente del paquete del proveedor detrás de la lista: ``call`` comprueba el destino
    del cliente antes de cada petición."""

    def __init__(self, client: T, check: Callable[[str], None]) -> None:
        self._client = client
        self._check = check

    async def call[R](self, operation: Callable[[T], Awaitable[R]]) -> R:
        """Ejecuta ``operation(cliente)`` si el destino del cliente sigue en la lista."""
        self._check(str(getattr(self._client, "base_url", "")))
        return await operation(self._client)


class HttpClientFactory:
    """La única forma de construir un cliente de salida en U-04."""

    def __init__(
        self,
        allowlist: EgressAllowlist,
        *,
        metrics: LoopMetrics | None = None,
        policy: redaction.AttributePolicy | None = None,
    ) -> None:
        self.allowlist = allowlist
        self._metrics = metrics if metrics is not None else get_loop_metrics()
        self._policy = policy if policy is not None else redaction.DEFAULT_POLICY

    # --- La comprobación ----------------------------------------------------------------------

    def _guard(self, component: EgressComponent, code_path: str) -> Callable[[str], None]:
        component = EgressComponent(component)
        if not isinstance(code_path, str) or _CODE_PATH.fullmatch(code_path) is None:
            raise ValueError("la ruta de código debe ser el módulo de U-04 que pide el cliente")
        # La ruta es una constante del código: así el registro crítico puede nombrarla.
        self._policy.register("code_path", [code_path])

        def check(url: str) -> None:
            if self.allowlist.allows(component, url):
                return
            self._metrics.loop_egress_blocked_total.add(1, {"egress_component": component})
            _log.critical(
                "salida bloqueada: destino fuera de la lista cerrada (LC-LAZ-39)",
                egress_component=component,
                code_path=code_path,
            )
            raise EgressBlocked(component, code_path)

        return check

    # --- Clientes -----------------------------------------------------------------------------

    def httpx_client(
        self,
        component: EgressComponent,
        code_path: str,
        *,
        timeout: httpx.Timeout | float,
        **options: Any,
    ) -> httpx.AsyncClient:
        """``httpx.AsyncClient`` hacia el host de ``component``; cada petición se comprueba.

        ``timeout`` es obligatorio (PAT-NUC-RES-03: adaptador con tiempo de espera).
        """
        owned = sorted(_HTTPX_OPTIONS_OWNED & options.keys())
        if owned:
            raise ValueError(f"opciones que fija la fábrica: {', '.join(owned)}")
        check = self._guard(component, code_path)

        async def on_request(request: httpx.Request) -> None:
            check(str(request.url))

        host = self.allowlist.host_for(EgressComponent(component))
        return httpx.AsyncClient(
            timeout=timeout,
            base_url=f"https://{host}",
            event_hooks={"request": [on_request]},
            follow_redirects=False,
            trust_env=False,
            **options,
        )

    def email_client(self, code_path: str, *, region_name: str, **options: Any) -> Any:
        """Cliente ``sesv2`` de boto3 contra el host del correo; cada envío se comprueba en
        ``before-send`` (también los reintentos de botocore)."""
        if "endpoint_url" in options:
            raise ValueError("opción que fija la fábrica: endpoint_url")
        check = self._guard(EgressComponent.EMAIL_SENDER, code_path)

        def before_send(request: Any, **_: Any) -> None:
            check(str(request.url))

        client = boto3.session.Session().client(
            "sesv2",
            region_name=region_name,
            endpoint_url=f"https://{self.allowlist.email_host}",
            **options,
        )
        client.meta.events.register("before-send", before_send)
        return client

    def assistant_client[T](
        self,
        constructor: Callable[..., T],
        code_path: str,
        **options: Any,
    ) -> GuardedClient[T]:
        """El cliente del paquete del proveedor con sus valores por omisión de transporte.

        ``constructor`` es la clase del paquete (``AsyncAnthropic``); la fábrica fija
        ``base_url`` al destino del asistente y **rechaza** ``http_client`` y cualquier objeto de
        ``httpx`` entre las opciones (tech-stack §2.1).
        """
        owned = sorted(_PROVIDER_OPTIONS_OWNED & options.keys())
        if owned:
            raise ValueError(f"opciones que fija la fábrica: {', '.join(owned)}")
        if any(isinstance(value, _HTTPX_OBJECTS) for value in options.values()):
            raise ValueError("el cliente del proveedor nunca recibe un objeto de httpx")
        check = self._guard(EgressComponent.ASSISTANT_PROVIDER, code_path)
        client = constructor(base_url=f"https://{self.allowlist.assistant_host}", **options)
        return GuardedClient(client, check)

    @classmethod
    def from_config(cls, config: RuntimeConfig | None) -> HttpClientFactory:
        return cls(EgressAllowlist.from_config(config))
