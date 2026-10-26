"""Canal de control del intermediario de fallos del mundo de U-05 (VIG-334)."""

from __future__ import annotations

import http.client
from urllib.parse import urlsplit

import pytest

from tests.e2e_u05.faults import fault_control
from tests.fault_proxy import ProxyMode, fault_proxy


def _post(url: str, path: str) -> int:
    address = urlsplit(url)
    connection = http.client.HTTPConnection(address.hostname or "", address.port, timeout=10)
    try:
        connection.request("POST", path)
        return connection.getresponse().status
    finally:
        connection.close()


def test_switches_the_proxy_mode_in_place() -> None:
    with fault_proxy("127.0.0.1", 9) as proxy, fault_control(proxy) as url:
        assert url.startswith("http://127.0.0.1:")
        for mode in (ProxyMode.FREEZE, ProxyMode.REFUSE, ProxyMode.FORWARD):
            assert _post(url, f"/mode/{mode.value}") == 204
            assert proxy.mode is mode


@pytest.mark.parametrize(
    "path", ["/mode/", "/mode/drop", "/mode/FREEZE", "/freeze", "/", "/exports/rewind"]
)
def test_rejects_anything_but_the_three_modes(path: str) -> None:
    with fault_proxy("127.0.0.1", 9) as proxy, fault_control(proxy) as url:
        assert _post(url, path) == 400
        assert proxy.mode is ProxyMode.FORWARD


def test_rewinds_the_export_window_only_on_its_exact_path() -> None:
    """VIG-356: cada escenario FS-APP empieza con el cupo de exportación de una hora nueva."""
    calls: list[None] = []
    with (
        fault_proxy("127.0.0.1", 9) as proxy,
        fault_control(proxy, rewind_exports=lambda: calls.append(None)) as url,
    ):
        for path in ("/exports/rewind/", "/exports/rewind?x=1", "/exports", "/mode/rewind"):
            assert _post(url, path) == 400
        assert calls == []
        assert _post(url, "/exports/rewind") == 204
        assert calls == [None]
        assert proxy.mode is ProxyMode.FORWARD


def test_a_failed_rewind_answers_500_without_detail() -> None:
    def fail() -> None:
        raise RuntimeError("detalle de la base que no sale del mundo")

    with fault_proxy("127.0.0.1", 9) as proxy, fault_control(proxy, rewind_exports=fail) as url:
        assert _post(url, "/exports/rewind") == 500
