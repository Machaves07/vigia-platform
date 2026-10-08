# Tipos mínimos de `weasyprint.urls` (ver `__init__.pyi`).
from collections.abc import Mapping

class URLFetchingError(IOError): ...
class FatalURLFetchingError(BaseException): ...

class URLFetcherResponse:
    url: str
    status: int
    def __init__(
        self,
        url: str,
        body: bytes | str | None = ...,
        headers: Mapping[str, str] | None = ...,
        status: int = ...,
    ) -> None: ...
    def read(self) -> bytes: ...
    def close(self) -> None: ...

class URLFetcher:
    def __init__(
        self,
        timeout: float = ...,
        *,
        allowed_protocols: frozenset[str] | set[str] | None = ...,
        allow_redirects: bool = ...,
        fail_on_errors: bool = ...,
    ) -> None: ...
    def fetch(self, url: str, headers: Mapping[str, str] | None = ...) -> URLFetcherResponse: ...
    def __call__(self, url: str) -> URLFetcherResponse: ...
