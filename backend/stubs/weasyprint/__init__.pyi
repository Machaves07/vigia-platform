# Tipos mínimos de WeasyPrint (no publica `py.typed`): solo lo que usa
# `vigia_platform.catalog.adapters.rendering` (TASK-217). Se amplían aquí, nunca con `Any` en el
# código. `mypy_path` de `pyproject.toml` incluye `stubs`.
from pathlib import Path

from weasyprint.text.fonts import FontConfiguration
from weasyprint.urls import URLFetcher

__version__: str
VERSION: str

class CSS:
    def __init__(
        self,
        *,
        string: str,
        base_url: str | Path | None = ...,
        url_fetcher: URLFetcher | None = ...,
        font_config: FontConfiguration | None = ...,
    ) -> None: ...

class HTML:
    def __init__(
        self,
        *,
        string: str,
        base_url: str | Path | None = ...,
        url_fetcher: URLFetcher | None = ...,
    ) -> None: ...
    def write_pdf(
        self,
        target: None = ...,
        *,
        stylesheets: list[CSS] | None = ...,
        font_config: FontConfiguration | None = ...,
        uncompressed_pdf: bool = ...,
    ) -> bytes: ...
