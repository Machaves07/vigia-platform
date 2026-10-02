"""FS-NUC-06 · Servicio de contraseñas filtradas caído (NFR-NUC-26; PAT-NUC-RES-03; LC-NUC-01).

**Inyección**: un servicio de rangos de filtradas **HTTP de verdad** (``/range/<prefijo>`` como el
real, con contraseñas sintéticas) detrás de un ``FaultProxy``: primero **latencia por encima de
3 s** (servicio congelado: acepta la conexión y no responde) y luego **rechazo de conexión**.
Las **altas** son activaciones de cuenta de verdad (``InvitationService.accept_invitation`` con
``PasswordService``: política de BR-NUC-20 y Argon2id real), sobre PostgreSQL como ``vigia_app``.

**Resultado esperado**: con el servicio lento o caído decide el **respaldo local**
(``LOCAL_FALLBACK``) y suma ``hibp_fallback_used``; **ninguna alta bloqueada** (las contraseñas que
no están en la lista local se aceptan y la cuenta queda activa, y con latencia la decisión llega
dentro del tope de 3 s más una holgura); **ninguna contraseña filtrada de la lista local
aceptada** (se rechaza con ``password_rejected`` y la invitación sigue sin usar).

Solo datos generados: contraseñas y correos sintéticos.
"""

from __future__ import annotations

import contextlib
import http.server
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest

from tests.fault_proxy import ProxyMode, fault_proxy
from tests.hibp_service import local_list, metric_total, metrics_with_reader
from tests.hierarchy_support import HierarchyEnvironment, hierarchy_environment, new_code, new_email
from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import WALL, scenario
from tests.session_support import GOOD_CODE
from vigia_platform.identity.adapters.hibp import HibpBreachChecker, sha1_hex
from vigia_platform.identity.application.common import IdentityRejected, IdentityRejection
from vigia_platform.identity.application.hierarchy import GenesisRequest, PlantSpec
from vigia_platform.identity.application.invitations import InvitationService
from vigia_platform.identity.auth.passwords import PasswordService
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.observability.metrics import MetricName

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

TIMEOUT_SECONDS: Final = 3.0
MARGIN_SECONDS: Final = 2.0
LOCAL_LEAK: Final = "contrasena-filtrada-local-1234"  # noqa: S105 - dato sintético
REMOTE_LEAK: Final = "contrasena-filtrada-remota-5678"  # noqa: S105 - dato sintético


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """``GET /range/<prefijo>`` con los sufijos de ``server.breached`` y una línea de relleno."""

    def do_GET(self) -> None:
        prefix = self.path.rsplit("/", 1)[-1].upper()
        breached: set[str] = self.server.breached  # type: ignore[attr-defined]
        lines = [f"{d[5:]}:7" for d in sorted(breached) if d[:5] == prefix] + [f"{'0' * 35}:0"]
        body = "\r\n".join(lines).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - firma de la base
        return


@contextlib.contextmanager
def range_service(breached: set[str]) -> Iterator[tuple[str, int]]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    server.breached = breached  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[HierarchyEnvironment]:
    with hierarchy_environment(postgres_endpoint, "fs_nuc_06") as environment:
        yield environment


@dataclass
class Activation:
    phase: str
    password_kind: str
    outcome: str
    seconds: float


def _invitation(env: HierarchyEnvironment) -> str:
    """Una organización nueva de la génesis: el token de la invitación de su administración."""
    result = env.run(
        env.genesis().create_client_organization(
            env.operator_context(),
            GenesisRequest(
                code=new_code("ORG"),
                name="Organización sintética",
                plant=PlantSpec(
                    new_code("PL"), "Planta sintética", "CO", "us-east-1", "America/Bogota"
                ),
                administrator_email=new_email("alta"),
                administrator_display_name="Administración sintética",
            ),
        )
    )
    link = result.invitation.link
    assert link is not None
    return str(link.split("#", 1)[1])


def _activate(
    env: HierarchyEnvironment, service: InvitationService, phase: str, kind: str, password: str
) -> Activation:
    token = _invitation(env)
    started = WALL.monotonic()
    try:
        env.run(
            service.accept_invitation(
                token, password, CURRENT_PRIVACY_NOTICE_VERSION, second_factor_code=GOOD_CODE
            )
        )
        outcome = "activated"
    except IdentityRejected as rejected:
        outcome = rejected.code.value
    seconds = WALL.monotonic() - started
    if outcome != "activated":
        # La invitación rechazada sigue sin usar: se puede activar con otra contraseña.
        assert env.run(service.begin_activation(token)) is not None
    return Activation(phase, kind, outcome, round(seconds, 2))


def test_fs_nuc_06_breached_password_service_slow_then_down(env: HierarchyEnvironment) -> None:
    with scenario(
        "FS-NUC-06",
        title="Servicio de contraseñas filtradas caído",
        injection="latencia inyectada por encima de 3 s y luego rechazo de conexión",
        expected=(
            "respaldo local; hibp_fallback_used; ninguna alta bloqueada ni contraseña filtrada de"
            " la lista local aceptada"
        ),
    ) as run:
        metrics, reader = metrics_with_reader()
        pool = CpuPool(SystemClock(), max_workers=1)
        activations: list[Activation] = []
        try:
            with range_service({sha1_hex(REMOTE_LEAK), sha1_hex(LOCAL_LEAK)}) as (host, port):
                with fault_proxy(host, port) as proxy:
                    checker = HibpBreachChecker(
                        local_list([LOCAL_LEAK]),
                        SystemClock(),
                        range_url=f"{proxy.url}/range/",
                        metrics=metrics,
                    )
                    service = InvitationService(
                        env.deps,
                        contexts=env.authz.contexts,
                        passwords=PasswordService(checker, pool),
                        second_factor=env.second_factor,
                    )
                    phases = [("latency", ProxyMode.FREEZE), ("refused", ProxyMode.REFUSE)]
                    # Con el servicio arriba decide el servicio (también una filtrada remota).
                    activations.append(_activate(env, service, "up", "remote_leak", REMOTE_LEAK))
                    for phase, mode in phases:
                        proxy.set_mode(mode)
                        order = ["fresh", "local_leak"]
                        run.random.shuffle(order)
                        for kind in order:
                            password = (
                                LOCAL_LEAK if kind == "local_leak" else f"nueva-{secrets.token_hex(8)}"
                            )
                            activations.append(_activate(env, service, phase, kind, password))
                    env.run(checker.aclose())
        finally:
            pool.shutdown()
        fallback_used = metric_total(reader, MetricName.HIBP_FALLBACK_USED)
        run.observe(
            activations=[activation.__dict__ for activation in activations],
            hibp_fallback_used=fallback_used,
        )
        rejected = IdentityRejection.PASSWORD_REJECTED.value
        by_key = {(a.phase, a.password_kind): a for a in activations}
        assert by_key[("up", "remote_leak")].outcome == rejected, "arriba decide el servicio"
        for phase in ("latency", "refused"):
            fresh = by_key[(phase, "fresh")]
            assert fresh.outcome == "activated", f"ninguna alta bloqueada ({phase})"
            # El tope de 3 s más una holgura (Argon2id y la transacción de la activación).
            assert fresh.seconds < TIMEOUT_SECONDS + MARGIN_SECONDS
            assert by_key[(phase, "local_leak")].outcome == rejected, phase
        assert fallback_used >= 2, "cada alta decidida por el respaldo suma hibp_fallback_used"
        # La latencia se inyectó de verdad: la alta esperó el tope antes de usar el respaldo.
        assert by_key[("latency", "fresh")].seconds >= TIMEOUT_SECONDS - 0.1
