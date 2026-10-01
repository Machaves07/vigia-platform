"""Claves de ``app.state`` que la fábrica (``shared.api.app``) fija para las rutas.

- ``PRIVACY_NOTICE_STATE_KEY``: la versión vigente del aviso de tratamiento, la misma que exige
  el paso ``PrivacyNoticeStep`` (``AppRuntime.privacy_notice_version``).
- ``API_VERSION_STATE_KEY``: la versión de la release servida, la ``app_version`` que publica
  ``/version.json`` (pendiente nº 7, adenda A-15).
"""

from __future__ import annotations

from typing import Final

__all__ = ["API_VERSION_STATE_KEY", "PRIVACY_NOTICE_STATE_KEY"]

PRIVACY_NOTICE_STATE_KEY: Final = "vigia_privacy_notice_version"
API_VERSION_STATE_KEY: Final = "vigia_api_version"
