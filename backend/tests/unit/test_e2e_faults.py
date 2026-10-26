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


@pytest.mark.parametrize("path", ["/mode/", "/mode/drop", "/mode/FREEZE", "/freeze", "/"])
def test_rejects_anything_but_the_three_modes(path: str) -> None:
    with fault_proxy("127.0.0.1", 9) as proxy, fault_control(proxy) as url:
        assert _post(url, path) == 400
        assert proxy.mode is ProxyMode.FORWARD
